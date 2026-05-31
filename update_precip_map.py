#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import requests
import json
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict

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
GRID_STEP = 0.2           # ~300 tačaka – dovoljno za finu mapu, a brzo na GitHubu
DAYS_TO_FETCH = 10

MIN_LAT, MAX_LAT = 42.5, 45.3
MIN_LON, MAX_LON = 15.7, 19.6

# ---------- FUNKCIJE ----------
def download_bih_border():
    """Preuzima GeoJSON granice BiH."""
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

@retry(wait=wait_exponential(multiplier=2, min=5, max=60), stop=stop_after_attempt(5))
def fetch_batch(latitudes, longitudes, start_date, end_date):
    """
    Šalje batch zahtjev Open-Meteo archive API-ju.
    Maksimalno 1000 lokacija po pozivu.
    """
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
    """
    Dohvata padavine za sve grid tačke koristeći batch pozive.
    Podijeli u grupe od po 1000 ako je potrebno.
    """
    records = []
    total = len(grid_points)
    # Podijeli u chunkove od max 950 (ostavimo marginu)
    chunk_size = 950
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
                # Indeks 0: precipitation_sum (jer smo samo to tražili)
                precip_vals = daily.Variables(0).ValuesAsNumpy()
                dates = pd.to_datetime(daily.Time(), unit='s').strftime('%Y-%m-%d')
                for j, date in enumerate(dates):
                    records.append({
                        'lat': lat,
                        'lon': lon,
                        'date': date,
                        'precipitation_sum': float(precip_vals[j]) if not np.isnan(precip_vals[j]) else 0.0
                    })
            print(f"  -> Uspješno obrađeno {len(chunk)} lokacija")
        except RetryError as e:
            print(f"  -> Neuspjeh nakon ponavljanja za chunk {i//chunk_size+1}: {e}")
        except Exception as e:
            print(f"  -> Greška za chunk {i//chunk_size+1}: {e}")
        time.sleep(1)  # mali odmor između chunkova
    print(f"Ukupno prikupljeno {len(records)} zapisa")
    return records

def create_timemap(records, border_path, output_path):
    """Kreira HeatMapWithTime mapu i sprema u HTML."""
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
        print(f"Greška pri preuzimanju granice: {e}, ali nastavljam ako fajl već postoji.")
    
    grid = generate_grid()
    records = fetch_all_data(grid, start_str, end_str)
    
    if not records:
        print("Nema prikupljenih podataka – izlazim.")
        return
    
    create_timemap(records, BORDER_FILENAME, OUTPUT_HTML)
    print("Zadatak završen.")

if __name__ == "__main__":
    main()
