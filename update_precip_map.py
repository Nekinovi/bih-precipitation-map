#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import time
import requests
from folium.plugins import Geocoder
from datetime import datetime, timedelta
from collections import defaultdict
from folium.plugins import HeatMapWithTime, Geocoder  # Dodano Geocoder ovdje
import folium
import numpy as np
import openmeteo_requests
import pandas as pd
from folium.plugins import HeatMapWithTime
from tenacity import retry as retry_dec, wait_exponential, stop_after_attempt
import requests_cache
from retry_requests import retry
import matplotlib
matplotlib.use("Agg")          # bitno za CI / GitHub Actions (nema displeja)
import matplotlib.pyplot as plt
import scipy.ndimage
import geojsoncontour
import random
from folium.plugins import TimestampedGeoJson
from scipy.interpolate import griddata
from matplotlib.colors import LinearSegmentedColormap, to_hex

cache_session = requests_cache.CachedSession('.cache', expire_after = -1)
retry_session = retry(cache_session, retries = 7, backoff_factor = 1.0)
# ---------- KONFIGURACIJA ----------
BIH_BORDER_URL = "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"
BORDER_FILENAME = "bi_border.geojson"
OUTPUT_HTML = "docs/index.html"
GRID_STEP = 0.0625
DAYS_TO_FETCH = 10

MIN_LAT, MAX_LAT = 42.5, 45.3
MIN_LON, MAX_LON = 15.7, 19.6

# ---------- FUNKCIJE ----------
def download_bih_border():
    resp = requests.get(BIH_BORDER_URL)
    resp.raise_for_status()
    countries = resp.json()
    bi_feature = None
    for f in countries['features']:
        if f['properties'].get('name') == 'Bosnia and Herzegovina':
            bi_feature = f
            break
    if not bi_feature:
        raise ValueError("GeoJSON za BiH nije pronađen")
    bi_geojson = {"type": "FeatureCollection", "features": [bi_feature]}
    with open(BORDER_FILENAME, 'w') as f:
        json.dump(bi_geojson, f)
    print(f"GeoJSON granice spremljen kao {BORDER_FILENAME}")

def generate_grid():
    points = []
    for lat in np.arange(MIN_LAT, MAX_LAT + GRID_STEP, GRID_STEP):
        for lon in np.arange(MIN_LON, MAX_LON + GRID_STEP, GRID_STEP):
            points.append({"lat": round(lat, 4), "lon": round(lon, 4)})
    print(f"Generirano {len(points)} grid tačaka")
    return points

@retry_dec(wait=wait_exponential(multiplier=2, min=10, max=70),
           stop=stop_after_attempt(5), reraise=True)
def fetch_batch(latitudes, longitudes):
    openmeteo = openmeteo_requests.Client(session=retry_session)
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitudes,
        "longitude": longitudes,
        "daily": "precipitation_sum",
        "past_days": DAYS_TO_FETCH,
        "forecast_days": 0,
        "timezone": "Europe/Sarajevo",
        
    }
    return openmeteo.weather_api(url, params=params)

def _process_responses(responses, records):
    for resp in responses:
        lat = resp.Latitude(); lon = resp.Longitude()
        daily = resp.Daily()
        precip = daily.Variables(0).ValuesAsNumpy()
        start_ts = daily.Time(); end_ts = daily.TimeEnd()
        step_sec = daily.Interval() if daily.Interval() > 0 else 86400
        dates, current_ts = [], start_ts
        while current_ts < end_ts:
            dates.append(datetime.utcfromtimestamp(current_ts + 43200).strftime('%Y-%m-%d'))
            current_ts += step_sec
        for j, date_str in enumerate(dates):
            if j < len(precip):
                records.append({
                    'lat': float(lat), 'lon': float(lon), 'date': date_str,
                    'precipitation_sum': float(precip[j]) if not np.isnan(precip[j]) else 0.0
                })

def _try_chunk(chunk, records):
    lats = [p['lat'] for p in chunk]; lons = [p['lon'] for p in chunk]
    try:
        responses = fetch_batch(lats, lons)
        _process_responses(responses, records)
        return True
    except Exception as e:
        print(f"  -> Batch pao (nakon retry-ja): {e}")
        return False
# --- ograničenje tempa: Open-Meteo dozvoljava 600 poziva/min,
#     a kod multi-lokacije svaka lokacija = 1 poziv ---
_RATE_LIMIT_PER_MIN = 500          # margina ispod 600
_sent_log = []                     # (timestamp, broj_lokacija)

def _respect_rate_limit(n_next):
    now = time.time()
    while _sent_log and now - _sent_log[0][0] >= 60:
        _sent_log.pop(0)
    used = sum(n for _, n in _sent_log)
    if used + n_next > _RATE_LIMIT_PER_MIN and _sent_log:
        wait = 60 - (now - _sent_log[0][0]) + 1
        if wait > 0:
            print(f"  (rate-limit pauza ~{wait:.0f}s da ostanem ispod 600/min)")
            time.sleep(wait)
    _sent_log.append((time.time(), n_next))

def fetch_all_data(grid_points, start_date=None, end_date=None):
    records = []
    chunk_size = 150
    chunks = [grid_points[i:i+chunk_size] for i in range(0, len(grid_points), chunk_size)]
    print(f"Ukupno {len(chunks)} batcheva po {chunk_size} lokacija")

    failed = []
    for i, chunk in enumerate(chunks):
        print(f"Šaljem batch {i+1}/{len(chunks)}...")
        _respect_rate_limit(len(chunk))          # <-- DODATI
        if _try_chunk(chunk, records):
            print(f"  -> OK ({len(chunk)} lokacija)")
        else:
            failed.append(chunk)
        time.sleep(3 + random.random() * 2)
    # drugi i treći prolaz za neuspjele
    for p in range(2):
        if not failed:
            break
        print(f"Ponovni prolaz {p+1} za {len(failed)} neuspjelih batcheva...")
        time.sleep(20)
        still = []
        for chunk in failed:
            _respect_rate_limit(len(chunk))      # <-- DODATI
            if not _try_chunk(chunk, records):
                still.append(chunk)
            time.sleep(5)
        failed = still

    if failed:
        print(f"UPOZORENJE: {len(failed)} batcheva trajno neuspjelo")

    print(f"Ukupno prikupljeno {len(records)} zapisa")
    unique_dates = sorted(set(r['date'] for r in records))
    print(f"Pronađeno dana: {len(unique_dates)} -> {unique_dates}")
    return records


 
 
def _build_contour_geojson(points_dict, levels, colors, lon_f, lat_f, LON, LAT):
    """Vrati listu GeoJSON featura (konture) za jedno polje vrijednosti."""
    pts = np.array(list(points_dict.keys()))      # [lat, lon]
    vals = np.array(list(points_dict.values()))
    if len(pts) < 4:
        return []
    Z = griddata(pts[:, [1, 0]], vals, (LON, LAT), method="linear")
    Z = np.clip(Z, 0, None)
    fig, ax = plt.subplots()
    cs = ax.contourf(lon_f, lat_f, Z, levels=levels, colors=colors, extend="max")
    plt.close(fig)
    gj = json.loads(geojsoncontour.contourf_to_geojson(
        contourf=cs, ndigits=3, fill_opacity=0.55))
    feats = []
    for feat in gj["features"]:
        p = feat["properties"]
        feat["properties"] = {
            "style": {
                "color": p.get("stroke", "#555"),
                "weight": 0.4,
                "fillColor": p.get("fill", "#3186cc"),
                "fillOpacity": float(p.get("fill-opacity", 0.55)),
            }
        }
        feats.append(feat)
    return feats
 
 
def create_timemap(records, border_path, output_path):
    # --- grupisanje po datumu: kljuc (lat, lon) -> padavine ---
    by_date = defaultdict(dict)
    for r in records:
        key = (round(float(r['lat']), 4), round(float(r['lon']), 4))
        by_date[r['date']][key] = float(r['precipitation_sum'])
    index = sorted(by_date.keys())
    print(f"Index (datumi za slider): {index}")
 
    # --- DNEVNI nivoi/boje ---
    levels = [0.5, 1, 2, 5, 10, 15, 20, 30, 40, 60, 80, 100, 150, 200, 300]
    colors = ['#f0f0f7', '#c5d8f2', '#7aa8ec', '#2f6fe0', '#1f3fb0',
                '#00a0c0', '#19b34d', '#8ec63f', '#ffe000', '#ffa000',
                '#ff5a00', '#e00000', '#a00030', '#c000a0', '#7a0050']
 
    # --- AKUMULIRANI nivoi (kao na pravim mapama) + paleta ---
    cum_levels = [0.1, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 40, 50, 60, 70,
                  80, 90, 100, 125, 150, 175, 200, 250, 300, 400, 500]
    _anchors = ['#f0f0f7', '#c5d8f2', '#7aa8ec', '#2f6fe0', '#1f3fb0',
                '#00a0c0', '#19b34d', '#8ec63f', '#ffe000', '#ffa000',
                '#ff5a00', '#e00000', '#a00030', '#c000a0', '#7a0050']
    _cmap = LinearSegmentedColormap.from_list("precip_cum", _anchors, N=256)
    cum_colors = [to_hex(_cmap(i / (len(cum_levels) - 1))) for i in range(len(cum_levels))]
 
    # fina ciljna mreza preko BiH bounding boxa
    lon_f = np.linspace(MIN_LON, MAX_LON, 220)
    lat_f = np.linspace(MIN_LAT, MAX_LAT, 220)
    LON, LAT = np.meshgrid(lon_f, lat_f)
 
    # ---- DNEVNI sloj (vremenski slider) ----
    daily_features = []
    for d in index:
        for feat in _build_contour_geojson(by_date[d], levels, colors, lon_f, lat_f, LON, LAT):
            feat["properties"]["times"] = [d]
            daily_features.append(feat)
 
    # ---- AKUMULIRANI sloj (suma svih dana po tacki) ----
    cumulative = defaultdict(float)
    for d in index:
        for key, v in by_date[d].items():
            cumulative[key] += v
    cum_features = _build_contour_geojson(dict(cumulative), cum_levels, cum_colors,
                                          lon_f, lat_f, LON, LAT)
    if cumulative:
        print(f"Akumulirano: {len(cum_features)} kontura, max suma ~{round(max(cumulative.values()),1)} mm")
 
    # --- mapa ---
    m = folium.Map(location=[44.15, 17.80], zoom_start=8, tiles="OpenStreetMap")
    try:
        folium.GeoJson(
            border_path, name='BiH Border',
            style_function=lambda x: {'color': 'black', 'weight': 2, 'fillOpacity': 0}
        ).add_to(m)
    except FileNotFoundError:
        print("Upozorenje: GeoJSON granice nije pronađen")
 
    ts = TimestampedGeoJson(
        {"type": "FeatureCollection", "features": daily_features},
        period="P1D", duration="P1D", transition_time=600,
        auto_play=False, loop=False, date_options="YYYY-MM-DD",
    )
    ts.add_to(m)
 
    cum_fg = folium.FeatureGroup(name="Akumulirano 10 dana", show=False)
    folium.GeoJson(
        {"type": "FeatureCollection", "features": cum_features},
        style_function=lambda f: f["properties"]["style"],
    ).add_to(cum_fg)
    cum_fg.add_to(m)
 
    # --- legenda DNEVNA ---
    daily_legend = '''
    <div id="legend_daily" style="position: fixed; bottom: 100px; left: 20px; width: 170px; background:white; border:2px solid grey; z-index:9999; opacity:0.92; padding:8px; font-size:13px; line-height: 18px;">
        <b>Dnevne padavine (mm)</b><br>
        <i style="background:#f0f0f7; display:inline-block; width:12px; height:12px; margin-right:5px; border:1px solid #ccc;"></i> 0.5 – 1<br>
        <i style="background:#c5d8f2; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 1 – 2<br>
        <i style="background:#7aa8ec; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 2 – 5<br>
        <i style="background:#2f6fe0; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 5 – 10<br>
        <i style="background:#1f3fb0; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 10 – 15<br>
        <i style="background:#00a0c0; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 15 – 20<br>
        <i style="background:#19b34d; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 20 – 30<br>
        <i style="background:#8ec63f; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 30 – 40<br>
        <i style="background:#ffe000; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 40 – 60<br>
        <i style="background:#ffa000; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 60 – 80<br>
        <i style="background:#ff5a00; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 80 – 100<br>
        <i style="background:#e00000; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 100 – 150<br>
        <i style="background:#a00030; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 150 – 200<br>
        <i style="background:#c000a0; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> 200 – 300<br>
        <i style="background:#7a0050; display:inline-block; width:12px; height:12px; margin-right:5px;"></i> &gt; 300<br>
    </div>
    '''

    grad_stops = ", ".join(
        f"{c} {round(i/(len(cum_colors)-1)*100)}%" for i, c in enumerate(cum_colors))
    cum_legend = f'''
    <div id="legend_cum" style="display:none; position: fixed; bottom: 100px; left: 20px; width: 360px; background:white; border:2px solid grey; z-index:9999; opacity:0.95; padding:8px; font-size:12px;">
        <b>Akumulirano 10 dana (mm)</b>
        <div style="height:14px; margin:6px 0; background:linear-gradient(to right, {grad_stops}); border:1px solid #888;"></div>
        <div style="display:flex; justify-content:space-between;">
            <span>0.1</span><span>5</span><span>20</span><span>50</span><span>100</span><span>200</span><span>500</span>
        </div>
    </div>
    '''
    toggle_btn = '''
    <div style="position: fixed; bottom: 50px; left: 20px; z-index:10000;">
        <button id="toggle_mode" style="padding:8px 12px; font-size:13px; cursor:pointer;
            background:#fff; border:2px solid #444; border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,.3);">
            Prikaz: Dnevno → klik za Akumulirano
        </button>
    </div>
    '''
    for el in (daily_legend, cum_legend, toggle_btn):
        m.get_root().html.add_child(folium.Element(el))
 
    m.get_root().header.add_child(folium.Element(
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'))
 
    # --- JS: toggle dnevno <-> akumulirano ---
    toggle_js = """
    window.addEventListener('load', function () {
        var t = 0;
        (function wait() {
            if (typeof TS_LAYER !== 'undefined' && typeof CUMUL_LAYER !== 'undefined'
                && typeof MAP_VAR !== 'undefined' && document.getElementById('toggle_mode')) {
                init();
            } else if (t++ < 150) { setTimeout(wait, 100); }
        })();
        function init() {
            var map = MAP_VAR, btn = document.getElementById('toggle_mode');
            var legD = document.getElementById('legend_daily');
            var legC = document.getElementById('legend_cum');
            var mode = 'daily';
            function timeCtrl(show) {
                document.querySelectorAll('.leaflet-control-timecontrol')
                    .forEach(function (e) { e.style.display = show ? '' : 'none'; });
            }
            function apply() {
                if (mode === 'daily') {
                    if (!map.hasLayer(TS_LAYER)) map.addLayer(TS_LAYER);
                    if (map.hasLayer(CUMUL_LAYER)) map.removeLayer(CUMUL_LAYER);
                    timeCtrl(true);
                    if (legD) legD.style.display = 'block';
                    if (legC) legC.style.display = 'none';
                    btn.innerHTML = 'Prikaz: Dnevno → klik za Akumulirano';
                } else {
                    if (map.hasLayer(TS_LAYER)) map.removeLayer(TS_LAYER);
                    if (!map.hasLayer(CUMUL_LAYER)) map.addLayer(CUMUL_LAYER);
                    timeCtrl(false);
                    if (legD) legD.style.display = 'none';
                    if (legC) legC.style.display = 'block';
                    btn.innerHTML = 'Prikaz: Akumulirano → klik za Dnevno';
                }
            }
            btn.onclick = function () { mode = (mode === 'daily') ? 'cum' : 'daily'; apply(); };
            apply();
        }
    });
    """.replace("TS_LAYER", ts.get_name()) \
       .replace("CUMUL_LAYER", cum_fg.get_name()) \
       .replace("MAP_VAR", m.get_name())
    m.get_root().script.add_child(folium.Element(toggle_js))
 
    # --- JS: geocoder + popup izvjestaj (tvoj postojeci blok) ---
    report_js = """
    window.addEventListener('load', function() {
        var t = 0;
        (function waitL() {
            if (window.L && typeof MAP_VAR !== 'undefined') {
                if (typeof L.Control.geocoder === 'function') { start(); }
                else {
                    var css = document.createElement('link');
                    css.rel = 'stylesheet';
                    css.href = 'https://unpkg.com/leaflet-control-geocoder/dist/Control.Geocoder.css';
                    document.head.appendChild(css);
                    var s = document.createElement('script');
                    s.src = 'https://unpkg.com/leaflet-control-geocoder/dist/Control.Geocoder.js';
                    s.onload = start;
                    s.onerror = function(){ console.error('Geocoder lib se ne moze ucitati'); };
                    document.head.appendChild(s);
                }
            } else if (t++ < 100) { setTimeout(waitL, 100); }
            else { console.error('Leaflet ili mapa nisu spremni'); }
        })();
        function start() {
            var map = MAP_VAR;
            var reportMarker = null;
            var geocoder = L.Control.geocoder({
                defaultMarkGeocode: false, collapsed: false, placeholder: 'Pretrazi lokaciju...'
            }).addTo(map);
            geocoder.on('markgeocode', function(e) {
                var c = e.geocode.center;
                map.setView(c, 10);
                showReport(c.lat, c.lng, e.geocode.name);
            });
            function showReport(lat, lon, name) {
                if (reportMarker) { map.removeLayer(reportMarker); }
                reportMarker = L.marker([lat, lon]).addTo(map);
                var cid = 'chart_' + Date.now();
                var html = '<div style="width:280px"><b>' + (name || 'Lokacija') + '</b>'
                    + '<div style="font-size:12px;color:#555">Padavine, zadnjih 10 dana</div>'
                    + '<canvas id="' + cid + '" width="280" height="170"></canvas>'
                    + '<div id="' + cid + '_t" style="font-size:13px;margin-top:4px"></div></div>';
                reportMarker.bindPopup(html, {minWidth: 300}).openPopup();
                var url = 'https://api.open-meteo.com/v1/forecast?latitude=' + lat
                    + '&longitude=' + lon
                    + '&daily=precipitation_sum&past_days=10&forecast_days=1&timezone=Europe%2FSarajevo';
                fetch(url).then(function(r){ return r.json(); }).then(function(data) {
                    var today = new Date().toISOString().slice(0,10);
                    var labels = [], vals = [];
                    for (var i = 0; i < data.daily.time.length; i++) {
                        if (data.daily.time[i] < today) {
                            labels.push(data.daily.time[i].slice(5));
                            vals.push(data.daily.precipitation_sum[i] || 0);
                        }
                    }
                    labels = labels.slice(-10); vals = vals.slice(-10);
                    var total = vals.reduce(function(a,b){ return a+b; }, 0);
                    var ctx = document.getElementById(cid);
                    if (!ctx || typeof Chart === 'undefined') return;
                    new Chart(ctx, {
                        type: 'bar',
                        data: { labels: labels, datasets: [{ data: vals, backgroundColor: '#4169E1' }] },
                        options: { plugins: { legend: { display: false } },
                            scales: { y: { beginAtZero: true, title: { display: true, text: 'mm' } } } }
                    });
                    document.getElementById(cid + '_t').innerHTML = '<b>Ukupno: ' + total.toFixed(1) + ' mm</b>';
                }).catch(function(){
                    var el = document.getElementById(cid + '_t');
                    if (el) el.innerHTML = 'Greska pri dohvatu podataka.';
                });
            }
        }
    });
    """.replace("MAP_VAR", m.get_name())
    m.get_root().script.add_child(folium.Element(report_js))
 
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    m.save(output_path)
    print(f"Mapa sačuvana kao {output_path}")

def main():
    print("Pokrećem automatsko ažuriranje karte padavina...")
    end_date = datetime.now() - timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_TO_FETCH - 1)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')
    print(f"Period: {start_str} – {end_str}")
    
    try:
        download_bih_border()
    except Exception as e:
        print(f"Greška pri preuzimanju granice: {e}")
    
    grid = generate_grid()
    records = fetch_all_data(grid, start_str, end_str)
    
    if not records:
        print("Nema podataka – izlazim.")
        return
    
    create_timemap(records, BORDER_FILENAME, OUTPUT_HTML)
    print("Zadatak završen.")

if __name__ == "__main__":
    main()
