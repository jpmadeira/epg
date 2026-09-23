#!/usr/bin/env python3
"""
EPG automático a partir da sua playlist M3U, usando os raspadores do iptv-org/epg.

  python build_channels.py build <url-ou-arquivo.m3u>
      -> gera channels.xml (entrada do grabber), idmap.json e faltando.txt

  python build_channels.py rename raw.xml guide.xml.gz
      -> troca os ids internos pelos tvg-id da sua playlist e compacta
"""
import copy
import gzip
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Ordem de prioridade: a primeira fonte que tiver o canal é usada.
SITES = [
    "claro.com.br",
    "vivoplay.com.br",
    "clarotvmais.com.br",
    "mi.tv",
    "meuguia.tv",
    "guiadetv.com",
]
EPG_DIR = Path("epg")            # clone do github.com/iptv-org/epg
MAP_FILE = Path("idmap.json")
MISSING_FILE = Path("faltando.txt")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)                       # remove [ALT], (BR) etc.
    s = re.sub(r"\b(fhd|uhd|hd|sd|4k|h265|hevc|alt|backup)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def read_m3u(src: str) -> list[dict]:
    if src.startswith("http"):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "ignore")
    else:
        text = Path(src).read_text("utf-8", "ignore")

    seen, chans = set(), []
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        tid = re.search(r'tvg-id="([^"]*)"', line)
        tname = re.search(r'tvg-name="([^"]*)"', line)
        title = line.rsplit(",", 1)[-1].strip()
        tvg_id = tid.group(1).strip() if tid else ""
        name = (tname.group(1).strip() if tname and tname.group(1).strip() else title)
        out_id = tvg_id or name
        if out_id and out_id not in seen:
            seen.add(out_id)
            chans.append({"tvg_id": tvg_id, "name": name, "out_id": out_id})
    return chans


def load_sites() -> dict:
    sites = {}
    for site in SITES:
        f = EPG_DIR / "sites" / site / f"{site}.channels.xml"
        if not f.exists():
            print(f"aviso: {f} não encontrado, pulando")
            continue
        by_id, by_name = {}, {}
        for ch in ET.parse(f).getroot().iter("channel"):
            xid = (ch.get("xmltv_id") or "").lower()
            if xid:
                by_id.setdefault(xid, ch)
                by_id.setdefault(norm(xid.split(".")[0]), ch)   # "GloboSP.br" -> "globosp"
            by_name.setdefault(norm(ch.text), ch)
        sites[site] = (by_id, by_name)
    return sites


def find(info: dict, sites: dict):
    keys_id = [info["tvg_id"].lower(), norm(info["tvg_id"])] if info["tvg_id"] else []
    keys_name = [norm(info["name"]), norm(info["tvg_id"])]
    for site, (by_id, by_name) in sites.items():
        for k in keys_id:
            if k and k in by_id:
                return site, by_id[k]
        for k in keys_name:
            if k and k in by_name:
                return site, by_name[k]
    return None


def build(src: str):
    playlist = read_m3u(src)
    sites = load_sites()

    root = ET.Element("channels")
    internal_of = {}          # (site, site_id) -> id interno
    idmap, missing = {}, []

    for info in playlist:
        hit = find(info, sites)
        if not hit:
            missing.append(info["name"])
            continue
        site, ch = hit
        key = (site, ch.get("site_id"))
        if key not in internal_of:
            internal = f"ch{len(internal_of):04d}"
            internal_of[key] = internal
            el = ET.SubElement(root, "channel", {
                "site": site,
                "lang": ch.get("lang", "pt"),
                "xmltv_id": internal,
                "site_id": ch.get("site_id"),
            })
            el.text = ch.text
        idmap.setdefault(internal_of[key], []).append(
            {"id": info["out_id"], "name": info["name"], "site": site}
        )

    ET.indent(root)
    ET.ElementTree(root).write("channels.xml", encoding="utf-8", xml_declaration=True)
    MAP_FILE.write_text(json.dumps(idmap, ensure_ascii=False, indent=1), "utf-8")
    MISSING_FILE.write_text("\n".join(sorted(missing)) + "\n", "utf-8")

    total = len(playlist)
    print(f"{total - len(missing)}/{total} canais encontrados; "
          f"{len(internal_of)} grades distintas; {len(missing)} em faltando.txt")


def rename(raw: str, out: str):
    idmap = json.loads(MAP_FILE.read_text("utf-8"))
    opener = gzip.open if raw.endswith(".gz") else open
    with opener(raw, "rb") as f:
        src = ET.parse(f).getroot()

    tv = ET.Element("tv", {"generator-info-name": "epg-auto"})
    chans = {c.get("id"): c for c in src.findall("channel")}

    for internal, targets in idmap.items():
        base = chans.get(internal)
        if base is None:
            continue
        for t in targets:
            c = copy.deepcopy(base)
            c.set("id", t["id"])
            for dn in c.findall("display-name"):
                c.remove(dn)
            dn = ET.Element("display-name", {"lang": "pt"})
            dn.text = t["name"]
            c.insert(0, dn)
            tv.append(c)

    n = 0
    for p in src.findall("programme"):
        for t in idmap.get(p.get("channel"), []):
            np_ = copy.deepcopy(p)
            np_.set("channel", t["id"])
            tv.append(np_)
            n += 1

    data = ET.tostring(tv, encoding="utf-8", xml_declaration=True)
    with gzip.open(out, "wb") as f:
        f.write(data)
    print(f"{out}: {len(tv.findall('channel'))} canais, {n} programas")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        build(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "rename":
        rename(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
