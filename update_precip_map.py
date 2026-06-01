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
from tenacity import retry, wait_exponential, stop_after_attempt, RetryError

# ---------- KONFIGURACIJA ----------
BIH_BORDER_URL = "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"
BORDER_FILENAME = "bi_border.geojson"
OUTPUT_HTML = "docs/index.html"
GRID_STEP = 0.05
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

@retry(wait=wait_exponential(multiplier=2, min=5, max=60), stop=stop_after_attempt(5))
def fetch_batch(latitudes, longitudes, start_date, end_date):
    """Batch zahtjev prema archive API (historijski podaci)"""
    openmeteo = openmeteo_requests.Client()
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": latitudes,
        "longitude": longitudes,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "precipitation_sum",
        "timezone": "Europe/Sarajevo"
    }
    responses = openmeteo.weather_api(url, params=params)
    return responses

def fetch_all_data(grid_points, start_date, end_date):
    records = []
    total = len(grid_points)
    chunk_size = 180
    
    for i in range(0, total, chunk_size):
        chunk = grid_points[i:i+chunk_size]
        lats = [p['lat'] for p in chunk]
        lons = [p['lon'] for p in chunk]
        print(f"Šaljem batch {i//chunk_size+1}: {len(chunk)} lokacija...")
        try:
            responses = fetch_batch(lats, lons, start_date, end_date)
            for resp in responses:
                lat = resp.Latitude()
                lon = resp.Longitude()
                daily = resp.Daily()
                precip = daily.Variables(0).ValuesAsNumpy()
# --- ISPRAVLJENI DIO ZA GENERISANJE DATUMA ---
                start_ts = daily.Time()
                end_ts = daily.TimeEnd()
                step_sec = daily.Interval() if daily.Interval() > 0 else 86400
                
                # Generišemo sve datume za ovaj vremenski opseg
                dates = []
                current_ts = start_ts
                while current_ts < end_ts:
                    date_str = datetime.utcfromtimestamp(current_ts+43200).strftime('%Y-%m-%d')
                    dates.append(date_str)
                    current_ts += step_sec
                
                # Spajanje datuma sa vrijednostima padavina
                for j, date_str in enumerate(dates):
                    if j < len(precip):
                        records.append({
                            'lat': float(lat),  # Odmah pretvaramo u float da JavaScript ne pukne
                            'lon': float(lon),  # Odmah pretvaramo u float
                            'date': date_str,
                            'precipitation_sum': float(precip[j]) if not np.isnan(precip[j]) else 0.0
                        })
            print(f"  -> Uspješno obrađeno {len(chunk)} lokacija")
        except Exception as e:
            print(f"  -> Greška za batch: {e}")
        time.sleep(5)
    
    print(f"Ukupno prikupljeno {len(records)} zapisa")
    unique_dates = sorted(set(r['date'] for r in records))
    print(f"Pronađeno dana: {len(unique_dates)} -> {unique_dates}")
    return records

def create_timemap(records, border_path, output_path):
    data_by_date = defaultdict(list)
    for rec in records:
        date = rec['date']
        lat = float(rec['lat'])
        lon = float(rec['lon'])
        precip = float(rec['precipitation_sum'])
        data_by_date[date].append([lat, lon, min(precip / 10.0, 1.0)])
    
    index = sorted(data_by_date.keys())
    print(f"Index (datumi za slider): {index}")
    heat_data = [data_by_date[d] for d in index]
    
    m = folium.Map(location=[44.15, 17.80], zoom_start=8, tiles="OpenStreetMap")
    try:
        folium.GeoJson(
            border_path,
            name='BiH Border',
            style_function=lambda x: {'color': 'black', 'weight': 2, 'fillOpacity': 0}
        ).add_to(m)
    except FileNotFoundError:
        print("Upozorenje: GeoJSON granice nije pronađen")
    
    HeatMapWithTime(
        heat_data,
        index=index,
        auto_play=False,
        max_opacity=0.6,
        gradient={0.0: '#ADD8E6', 0.1: '#87CEEB', 0.5: '#4169E1', 1.0: '#8A2BE2'},
        radius=0.09,  # Prilagođeno za veći grid step
        blur=0.5,
        scale_radius=True,
        use_local_extrema=False
    ).add_to(m)
    
    legend_html = '''
    <div style="position: fixed; bottom: 50px; left: 50px; width: 150px; background:white; border:2px solid grey; z-index:9999; opacity:0.9; padding:6px;">
        <b>Dnevne padavine (mm)</b><br>
        <i style="background:#ADD8E6; display:inline-block; width:12px; height:12px;"></i> 0–1<br>
        <i style="background:#87CEEB; display:inline-block; width:12px; height:12px;"></i> 1–5<br>
        <i style="background:#4169E1; display:inline-block; width:12px; height:12px;"></i> 5–10<br>
        <i style="background:#8A2BE2; display:inline-block; width:12px; height:12px;"></i> >10
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))
    # Ugrađeni Folium Geocoder koji automatski pronalazi ispravnu varijablu mape
    m.get_root().header.add_child(folium.Element(
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'
    ))

    # Folium Geocoder - folium garantuje ispravan red ucitavanja

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
                defaultMarkGeocode: false,
                collapsed: false,
                placeholder: 'Pretrazi lokaciju...'
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
                        options: {
                            plugins: { legend: { display: false } },
                            scales: { y: { beginAtZero: true, title: { display: true, text: 'mm' } } }
                        }
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
