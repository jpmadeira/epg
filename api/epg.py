from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from html.parser import HTMLParser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re, xml.etree.ElementTree as ET

LINEUP = "https://www.lineup.tv.br/gdc.php?idCanal=8296"
TIMES = "https://timesbrasil.com.br/programacao/"
TZ = ZoneInfo("America/Sao_Paulo")

class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text=[]
        self.links=[]
    def handle_data(self, d):
        d=" ".join(d.split())
        if d: self.text.append(d)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=="a" and a.get("href"):
            self.links.append(a["href"])

def get(url):
    req=Request(url, headers={"User-Agent":"Mozilla/5.0 (EPG generator)"})
    with urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8","replace")

def extract_lineup():
    # O Line-UP pode ter a grade disponível em páginas/links diferentes.
    # Procuramos links de grade relacionados ao canal 8296.
    html=get(LINEUP)
    p=P(); p.feed(html)
    candidates=[urljoin(LINEUP,x) for x in p.links if "gdc" in x.lower()]
    pages=[LINEUP]+candidates
    found=[]
    for u in pages:
        try:
            h=get(u); q=P(); q.feed(h)
            s=" ".join(q.text)
            found += re.findall(r'(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})\s*-\s*([^|<]+)', s)
        except Exception:
            pass
    return found

def extract_times_programacao():
    # Fallback/segunda fonte: página oficial, que expõe as datas e horários.
    # O parser abaixo procura horários no HTML e os títulos próximos.
    html=get(TIMES)
    p=P(); p.feed(html)
    lines=p.text
    out=[]
    current_date=datetime.now(TZ).date()
    for i,line in enumerate(lines):
        m=re.fullmatch(r'(\d{2}):(\d{2})', line)
        if m:
            # procura o primeiro texto útil depois do horário
            title=None
            for x in lines[i+1:i+8]:
                if x and not re.fullmatch(r'\d{2}:\d{2}',x) and x not in ("LIVE","ASSISTIR LIVE"):
                    title=x; break
            if title:
                out.append((current_date.strftime("%d/%m/%Y"),
                            f"{m.group(1)}:{m.group(2)}:00", title))
    return out

def make_xml():
    # O Line-UP é a fonte prioritária; a página oficial é fallback.
    rows=extract_lineup()
    if not rows:
        rows=extract_times_programacao()

    # Deduplica e ordena.
    clean=[]
    seen=set()
    for d,t,title in rows:
        title=" ".join(title.split()).strip()
        key=(d,t,title)
        if title and key not in seen:
            seen.add(key); clean.append(key)

    # Se a fonte retornar somente o dia atual, não inventamos programas.
    # O endpoint pode ser chamado novamente para atualizar a grade.
    byday={}
    for d,t,title in clean:
        try:
            dt=datetime.strptime(f"{d} {t}","%d/%m/%Y %H:%M:%S").replace(tzinfo=TZ)
            byday.setdefault(dt.date(),[]).append((dt,title))
        except Exception:
            continue

    tv=ET.Element("tv", {"generator-info-name":"Line-UP / Times Brasil CNBC automatic EPG"})
    ch=ET.SubElement(tv,"channel",{"id":"TIMESBRASILCNBC"})
    ET.SubElement(ch,"display-name",{"lang":"pt"}).text="Times Brasil CNBC"
    ET.SubElement(ch,"icon",{"src":"https://www.lineup.net.br/canais/timesbrasilcnbc.gif"})
    ET.SubElement(ch,"url").text="https://timesbrasil.com.br/"

    for day in sorted(byday):
        items=sorted(byday[day])
        for n,(start,title) in enumerate(items):
            stop=items[n+1][0] if n+1<len(items) else start+timedelta(hours=1)
            def fmt(x): return x.strftime("%Y%m%d%H%M%S %z")
            pr=ET.SubElement(tv,"programme",{
                "channel":"TIMESBRASILCNBC",
                "start":fmt(start),
                "stop":fmt(stop)
            })
            ET.SubElement(pr,"title",{"lang":"pt"}).text=title

    ET.indent(tv,space="  ")
    return ET.tostring(tv,encoding="utf-8",xml_declaration=True)

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/api/epg.xml":
            self.send_response(404); self.end_headers(); return
        try:
            data=make_xml()
            self.send_response(200)
            self.send_header("Content-Type","application/xml; charset=utf-8")
            self.send_header("Cache-Control","s-maxage=21600, stale-while-revalidate=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("EPG error: "+str(e)).encode())
