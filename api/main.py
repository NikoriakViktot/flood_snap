from fastapi import FastAPI
import requests, os, subprocess

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

app = FastAPI()

USER = os.environ["CDSE_USER"]
PASS = os.environ["CDSE_PASS"]

def get_token():
    r = requests.post(TOKEN_URL, data={
        "grant_type": "password",
        "client_id": "cdse-public",
        "username": USER,
        "password": PASS
    })
    return r.json()["access_token"]

@app.post("/flood-event")
def flood_event(start: str, end: str):

    token = get_token()

    bbox = "POLYGON((24.7 47.6,25.2 47.6,25.2 48.2,24.7 48.2,24.7 47.6))"

    q = (
        "Collection/Name eq 'SENTINEL-1' and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{bbox}') and "
        f"ContentDate/Start gt {start}T00:00:00.000Z and "
        f"ContentDate/Start lt {end}T23:59:59.999Z"
    )

    r = requests.get(CATALOGUE, params={"$filter": q}).json()

    urls = []

    for p in r["value"][:3]:
        urls.append(f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({p['Id']})/$value")

    urlfile = "/data/urls.txt"

    with open(urlfile, "w") as f:
        for u in urls:
            f.write(u + "\n")

    subprocess.run([
        "docker","exec","flood_downloader",
        "aria2c","-x8","-s8","-c","-d","/data","-i","/data/urls.txt"
    ])

    return {"scenes": len(urls)}
