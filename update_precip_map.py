#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
print("DEBUG: Skripta je pokrenuta!", flush=True)
sys.stdout.flush()

import json
import requests
import time
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import folium
from folium.plugins import HeatMapWithTime
from tenacity import retry, wait_exponential, stop_after_attempt, RetryError

# ---------- KONFIGURACIJA ----------
BIH_BORDER_URL = "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"
BORDER_FILENAME = "bi_border.geojson"
OUTPUT_HTML = "docs/index.html"   # 👈 Ovo je ključno: spremamo u docs/index.html
GRID_STEP = 0.1
DAYS_TO_FETCH = 10

# Bounding box Bosne i Hercegovine (okvir unutar kojeg pravimo tačke)
MIN_LAT, MAX_LAT = 42.5, 45.3
MIN_LON, MAX_LON = 15.7, 19.6

# ---------- FUNKCIJE ----------
def download_bih_border():
    """Preuzima GeoJSON za BiH i sprema ga lokalno."""
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
    """Generiše grid tačaka unutar bounding box-a BiH."""
    points = []
    for lat in np.arange(MIN_LAT, MAX_LAT + GRID_STEP, GRID_STEP):
        for lon in np.arange(MIN_LON, MAX_LON + GRID_STEP, GRID_STEP):
            points.append({"lat": round(lat, 4), "lon": round(lon, 4)})
    print(f"Generirano {len(points)} grid tačaka")
    return points

@retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
def fetch_precip_for_point(lat, lon, start_date, end_date):
    """Dohvata dnevne padavine za jednu tačku sa Open-Meteo API."""
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&start_date={start_date}&end_date={end_date}"
           f"&daily=precipitation_sum&timezone=Europe/Sarajevo")
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()

def fetch_all_data(grid_points, start_date, end_date):
    """Petlja kroz sve grid tačke, prikuplja padavine za svaki dan u opsegu."""
    records = []
    total = len(grid_points)
    for idx, point in enumerate(grid_points):
        lat, lon = point['lat'], point['lon']
        try:
            data = fetch_precip_for_point(lat, lon, start_date, end_date)
            daily = data.get('daily', {})
            times = daily.get('time', [])
            precip_vals = daily.get('precipitation_sum', [])
            if not times or not precip_vals:
                print(f"Upozorenje: nema podataka za {lat},{lon}")
                continue
            for t, p in zip(times, precip_vals):
                records.append({
                    'lat': lat,
                    'lon': lon,
                    'date': t,
                    'precipitation_sum': p if p is not None else 0.0
                })
        except RetryError as e:
            print(f"Neuspjeh nakon ponavljanja za {lat},{lon}: {e}")
        except Exception as e:
            print(f"Greška za {lat},{lon}: {e}")
        # Pauza da ne preopteretimo API
        time.sleep(0.1)
        if (idx + 1) % 100 == 0:
            print(f"Procesirano {idx+1}/{total} tačaka")
    print(f"Ukupno prikupljeno {len(records)} zapisa")
    return records

def create_timemap(records, border_path, output_path):
    """Gradi HeatMapWithTime mapu i sprema u HTML."""
    data_by_date = defaultdict(list)
    for rec in records:
        date = rec['date']
        lat = float(rec['lat'])
        lon = float(rec['lon'])
        precip = float(rec['precipitation_sum'])
        data_by_date[date].append([lat, lon, precip])
    
    index = sorted(data_by_date.keys())
    heat_data = [data_by_date[d] for d in index]
    
    m = folium.Map(location=[44.15, 17.80], zoom_start=8, tiles="OpenStreetMap")
    # Dodaj granicu BiH
    try:
        folium.GeoJson(
            border_path,
            name='BiH Border',
            style_function=lambda x: {'color': 'black', 'weight': 2, 'fillOpacity': 0}
        ).add_to(m)
    except FileNotFoundError:
        print("Upozorenje: GeoJSON granice nije pronađen, prikazujem bez granice.")
    
    HeatMapWithTime(
        heat_data,
        index=index,
        auto_play=True,
        max_opacity=0.8,
        gradient={0.0: '#ADD8E6', 0.25: '#87CEEB', 0.5: '#4169E1', 0.75: '#8A28E2', 1.0: '#4B0B82'},
        radius=20,
        blur=15
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
    m.save(output_path)
    print(f"Mapa sačuvana kao {output_path}")

def main():
    print("Pokrećem automatsko ažuriranje karte padavina...")
    # 1. Datumi: posljednjih 10 dana (do jučer)
    end_date = datetime.now() - timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_TO_FETCH - 1)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')
    print(f"Period: {start_str} – {end_str}")
    
    # 2. Preuzmi granicu (prvi put, kasnije može već postojati)
    try:
        download_bih_border()
    except Exception as e:
        print(f"Greška pri preuzimanju granice: {e}, ali nastavljam ako fajl već postoji.")
    
    # 3. Generiši grid tačke (ili učitaj iz keša)
    grid = generate_grid()
    
    # 4. Dohvati podatke za sve tačke
    records = fetch_all_data(grid, start_str, end_str)
    
    if not records:
        print("Nema prikupljenih podataka – izlazim.")
        return
    
    # 5. Kreiraj mapu i spremi
    create_timemap(records, BORDER_FILENAME, OUTPUT_HTML)
    print("Zadatak završen.")

if __name__ == "__main__":
    main()
