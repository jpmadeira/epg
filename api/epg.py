from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from html.parser import HTMLParser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re
import xml.etree.ElementTree as ET

CHANNEL_ID = "TIMESBRASILCNBC"
CHANNEL_NAME = "Times Brasil CNBC"
LINEUP_URL = "https://www.lineup.tv.br/gdc.php?idCanal=8296"
TZ = ZoneInfo("America/Sao_Paulo")


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.links = []

    def handle_data(self, data):
        data = " ".join(data.split())
        if data:
            self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            href = attrs.get("href")
            if href:
                self.links.append(href)


def fetch(url):
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    with urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8", "replace")


def parse_program_lines(html):
    """
    O Line-UP apresenta a grade em linhas no formato:
      17/09/2026 15:30:00 - Nome do programa

    Retornamos (datetime, titulo).
    """
    parser = Parser()
    parser.feed(html)

    rows = []
    for text in parser.text:
        m = re.match(
            r"^(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})\s*-\s*(.+?)$",
            text,
        )
        if not m:
            continue

        date_s, time_s, title = m.groups()
        title = " ".join(title.split()).strip()
        try:
            dt = datetime.strptime(
                f"{date_s} {time_s}", "%d/%m/%Y %H:%M:%S"
            ).replace(tzinfo=TZ)
        except ValueError:
            continue

        if title:
            rows.append((dt, title))

    return rows


def collect_programs():
    # Uma única requisição à página do canal (idCanal já identifica o
    # canal certo). Nada de seguir links da página: isso disparava
    # dezenas de requisições extras e travava a função (timeout).
    html = fetch(LINEUP_URL)
    programs = parse_program_lines(html)

    # Remove duplicados.
    unique = {}
    for dt, title in programs:
        unique[(dt, title)] = (dt, title)

    now = datetime.now(TZ)
    limit = now + timedelta(days=7)

    result = [
        item for item in unique.values()
        if now - timedelta(hours=6) <= item[0] <= limit
    ]
    result.sort(key=lambda x: x[0])
    return result


def xml_time(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def build_xml(programs):
    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "Automatic Line-UP EPG",
            "generator-info-url": "https://www.lineup.tv.br/",
        },
    )

    channel = ET.SubElement(tv, "channel", {"id": CHANNEL_ID})
    ET.SubElement(
        channel, "display-name", {"lang": "pt"}
    ).text = CHANNEL_NAME

    # Logo: pode ser alterada facilmente se o endereço oficial da logo
    # mudar; a programação continua sendo obtida automaticamente.
    ET.SubElement(
        channel,
        "icon",
        {"src": "https://www.lineup.net.br/canais/timesbrasilcnbc.gif"},
    )
    ET.SubElement(
        channel, "url"
    ).text = "https://timesbrasil.com.br/"

    for i, (start, title) in enumerate(programs):
        if i + 1 < len(programs):
            stop = programs[i + 1][0]
        else:
            stop = start + timedelta(hours=1)

        # Evita duração negativa ou zero em caso de dados inconsistentes.
        if stop <= start:
            stop = start + timedelta(hours=1)

        programme = ET.SubElement(
            tv,
            "programme",
            {
                "channel": CHANNEL_ID,
                "start": xml_time(start),
                "stop": xml_time(stop),
            },
        )
        ET.SubElement(
            programme, "title", {"lang": "pt"}
        ).text = title

    ET.indent(tv, space="  ")
    return ET.tostring(tv, encoding="utf-8", xml_declaration=True)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            programs = collect_programs()

            if not programs:
                raise RuntimeError(
                    "Nenhuma programação foi encontrada no Line-UP."
                )

            data = build_xml(programs)

            self.send_response(200)
            self.send_header(
                "Content-Type", "application/xml; charset=utf-8"
            )
            # Cache por 6 horas. Assim o TiviMate não consulta o Line-UP
            # a cada abertura do aplicativo.
            self.send_header(
                "Cache-Control",
                "s-maxage=21600, stale-while-revalidate=86400",
            )
            self.end_headers()
            self.wfile.write(data)

        except Exception as exc:
            self.send_response(502)
            self.send_header(
                "Content-Type", "text/plain; charset=utf-8"
            )
            self.end_headers()
            self.wfile.write(
                f"EPG error: {exc}".encode("utf-8", "replace")
            )
