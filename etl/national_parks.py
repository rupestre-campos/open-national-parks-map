import geopandas as gpd
import pandas as pd
import osmnx as ox
import os
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from shapely.geometry import MultiPoint, MultiLineString, MultiPolygon
from shapely.validation import make_valid

ox.settings.max_query_area_size = 25_000_000_000_000
ox.settings.requests_timeout = 180

countries_url = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
output_folder = "./data"

def download_url(url, output_folder):
    """Download a file from a URL and save it to the specified output folder."""
    outfile = os.path.join(output_folder, os.path.basename(url))
    if os.path.exists(outfile):
        return outfile
    req = requests.get(url)
    if req.ok:
        with open(outfile, "wb") as file:
            file.write(req.content)
        return outfile
    raise ValueError(f"Failed to download {url}: {req.content}")


def load_countries(url, output_folder):
    """Download and load the zipped shapefile of countries into a GeoDataFrame."""
    zip_path = download_url(url, output_folder)
    countries_gdf = gpd.read_file(f"zip://{zip_path}")

    # Ensure CRS is EPSG:4326 for compatibility with OSMnx
    if countries_gdf.crs != "EPSG:4326":
        countries_gdf = countries_gdf.to_crs("EPSG:4326")

    return countries_gdf

def fetch_features(geometry, tags):
    """Query OSM features."""
    return ox.features_from_polygon(geometry, tags=tags)

def fetch_parks_for_country(row, tags):
    """Query OSM for parks within a country's boundary."""
    country_name = row["NAME"]
    geometry = row["geometry"]

    try:
        parks_gdf = fetch_features(geometry, tags=tags)
        parks_gdf["country"] = country_name
        print(country_name)
        return parks_gdf
    except Exception as e:
        print(f"Error retrieving parks for {country_name}: {e}")
        return None


def retrieve_parks_parallel(countries_gdf, tags, max_workers=4):
    """Retrieve parks for all countries in parallel."""
    parks_data = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_parks_for_country, row, tags): row["NAME"] for _, row in countries_gdf.iterrows()}
        for future in as_completed(futures):
            country_name = futures[future]
            try:
                result = future.result()
                if result is not None:
                    parks_data.append(result)
            except Exception as e:
                print(f"Error processing country {country_name}: {e}")
    return parks_data


def save_parks_to_file(parks_data, output_path):
    """Save the parks data to a GeoPackage with separate layers for points, lines, and polygons."""
    if not parks_data:
        print("No parks data to save.")
        return

    # Concatenate all parks into a single GeoDataFrame
    all_parks_gdf = gpd.GeoDataFrame(pd.concat(parks_data, ignore_index=True))

    # Ensure geometries are valid
    all_parks_gdf = all_parks_gdf[all_parks_gdf.geometry.notnull()]
    all_parks_gdf = all_parks_gdf.set_crs("EPSG:4326", allow_override=True)
    all_parks_gdf = rename_duplicated_columns(all_parks_gdf)
    # Handle duplicate column names
    all_parks_gdf = rename_duplicated_columns(all_parks_gdf)

    # Simplify the GeoDataFrame: keep 'name' and 'geometry', move others to JSON
    def create_properties_json(row):
        props = row.drop(["name", "geometry"]).to_dict()
        return json.dumps(props)

    all_parks_gdf["properties"] = all_parks_gdf.apply(create_properties_json, axis=1)
    all_parks_gdf = all_parks_gdf[["name", "geometry", "properties"]].copy()

    # Separate geometries into points, lines, and polygons
    points_gdf = all_parks_gdf[all_parks_gdf.geometry.type.isin(["Point", "MultiPoint"])]
    lines_gdf = all_parks_gdf[all_parks_gdf.geometry.type.isin(["LineString", "MultiLineString"])]
    polygons_gdf = all_parks_gdf[all_parks_gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]

    # Convert to multi-geometry types
    points_gdf["geometry"] = points_gdf.geometry.apply(
        lambda geom: MultiPoint([geom]) if geom.geom_type == "Point" else geom
    )
    lines_gdf["geometry"] = lines_gdf.geometry.apply(
        lambda geom: MultiLineString([geom]) if geom.geom_type == "LineString" else geom
    )
    polygons_gdf["geometry"] = polygons_gdf.geometry.apply(
        lambda geom: MultiPolygon([geom]) if geom.geom_type == "Polygon" else geom
    )

    # Save each layer to a GeoPackage
    points_gdf.to_file(output_path, layer="points", driver="GPKG")
    lines_gdf.to_file(output_path, layer="lines", driver="GPKG")
    polygons_gdf.to_file(output_path, layer="polygons", driver="GPKG")
    print(f"Saved parks data to {output_path} with layers: points, lines, polygons")

# Handle duplicated column names by renaming them instead of dropping
def rename_duplicated_columns(gdf):
    """Rename duplicated columns in a GeoDataFrame."""
    if 'fid' in gdf.columns:
        del gdf['fid']
    seen = set()
    new_columns = []
    for col in gdf.columns:
        new_col = col.lower().replace(":","_").replace(" ", "")
        count = 1
        while new_col in seen:
            new_col = f"{col.lower().replace(':','_').replace(' ', '')}_{count}"
            count += 1
        new_columns.append(new_col)
        seen.add(new_col)
    gdf.columns = new_columns
    return gdf

def main():
    os.makedirs(output_folder, exist_ok=True)
    output_path = os.path.join(output_folder, "national_parks.gpkg")
    tags = {
        "boundary": ["national_park"],
        "protect_class": ["2"],
        "designation": "national_park",
        "protected_area":"national_park"
    }

    countries_gdf = load_countries(countries_url, output_folder)

    countries_gdf["geometry"] = countries_gdf["geometry"].apply(
        lambda geom: make_valid(geom) if geom and not geom.is_valid else geom
    )

    parks_data = retrieve_parks_parallel(countries_gdf, tags, max_workers=4)

    save_parks_to_file(parks_data, output_path)


if __name__ == "__main__":
    main()
