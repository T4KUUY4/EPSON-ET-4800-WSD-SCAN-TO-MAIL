"""WSD push-scan to email.

Subscribes to the printer's WS-Scan ScanAvailableEvent with one "computer"
per configured destination. Pick a destination on the printer panel under
Scan -> Computer (WSD); the pages are fetched, combined into a PDF and emailed.
A small web UI picks the printer, shows its features, manages destinations
and shows status, scans and SOAP traffic.

Works with any WS-Scan (WSD) scanner: scan settings are fitted to what the
device reports (JPEG, uncompressed TIFF or PDF output; glass, feeder, duplex).

Python standard library only.
"""
import copy
import html
import json
import os
import re
import signal
import smtplib
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zlib
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape as esc

# --- configuration ----------------------------------------------------------

PRINTER_IP = os.environ.get("PRINTER_IP", "").strip()  # initial printer; changeable in the web UI
WSD_DEVICE_URL = os.environ.get("WSD_DEVICE_URL", "").strip()  # skip discovery
ADVERTISE_IP = os.environ.get("ADVERTISE_IP", "").strip()      # IP the printer calls back
WEB_PORT = int(os.environ.get("WEB_PORT", "8098"))
DATA = Path(os.environ.get("DATA_DIR", "/data"))
SCAN_DIR = DATA / "scans"
CONFIG_FILE = DATA / "config.json"

# Initial SMTP settings; after the first start they are edited in the web UI.
SMTP_DEFAULTS = {
    "host": os.environ.get("SMTP_HOST", ""),
    "port": os.environ.get("SMTP_PORT", "587"),
    "security": os.environ.get("SMTP_SECURITY", "starttls").lower(),
    "user": os.environ.get("SMTP_USER", ""),
    "password": os.environ.get("SMTP_PASS", ""),
    "from": os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", "")),
    "subject": os.environ.get("MAIL_SUBJECT", "Scan"),
}
MAIL_TO = os.environ.get("MAIL_TO", "")
SECURITY = {"starttls": "STARTTLS (usually port 587)", "ssl": "SSL/TLS (usually port 465)", "none": "None (port 25)"}

SUBSCRIPTION_TTL = "PT1H"
RENEW_EVERY = 300  # seconds; also how quickly a printer reboot is noticed

SOURCES = {"auto": "Auto (feeder, else glass)", "ADF": "Document feeder",
           "ADFDuplex": "Document feeder, both sides", "Platen": "Glass"}
COLORS = {"RGB24": "Color", "Grayscale8": "Grayscale", "BlackAndWhite1": "Black & white"}
DPIS = ["150", "200", "300", "600"]
PAPERS = {"A4": (8268, 11690), "Letter": (8500, 11000), "Legal": (8500, 14000)}  # 1/1000 inch
JPEG_FORMATS = ("jfif", "exif")
TIFF_FORMAT = "tiff-single-uncompressed"
PDF_FORMATS = ("pdf-a", "pdf")

# --- WSD constants ----------------------------------------------------------

NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wsd": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "wse": "http://schemas.xmlsoap.org/ws/2004/08/eventing",
    "wsdp": "http://schemas.xmlsoap.org/ws/2006/02/devprof",
    "sca": "http://schemas.microsoft.com/windows/2006/08/wdp/scan",
}
SCA = NS["sca"]
WSE = NS["wse"]
A_PROBE = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
A_GET = "http://schemas.xmlsoap.org/ws/2004/09/transfer/Get"
A_SUBSCRIBE = WSE + "/Subscribe"
A_RENEW = WSE + "/Renew"
A_UNSUBSCRIBE = WSE + "/Unsubscribe"
A_SUBSCRIPTION_END = WSE + "/SubscriptionEnd"
A_SCAN_AVAILABLE = SCA + "/ScanAvailableEvent"
A_CREATE_JOB = SCA + "/CreateScanJob"
A_RETRIEVE = SCA + "/RetrieveImage"
A_CANCEL = SCA + "/CancelJob"
A_GET_ELEMENTS = SCA + "/GetScannerElements"
ET.register_namespace("wscn", SCA)
ANONYMOUS = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
DISCOVERY_TO = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"

# --- logging ----------------------------------------------------------------

LOG = deque(maxlen=200)
TRAFFIC = deque(maxlen=40)
JOBS = deque(maxlen=50)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"{now()}  {msg}"
    LOG.append(line)
    print(line, flush=True)


def trace(direction, what, data):
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    TRAFFIC.append((now(), direction, what, data[:30000]))


# --- config -----------------------------------------------------------------

CFG_LOCK = threading.Lock()
CFG = {}


def printer_ip():
    """The selected printer: chosen in the web UI, else PRINTER_IP from the environment."""
    return CFG.get("printer_ip") or PRINTER_IP


def new_destination(name="Scan to Email", to=MAIL_TO):
    return {"id": uuid.uuid4().hex[:8], "name": name, "to": to, "source": "auto",
            "color": "RGB24", "dpi": "300", "paper": "A4"}


def load_config():
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    else:
        cfg = {"destinations": [new_destination()]}
    cfg["smtp"] = {**SMTP_DEFAULTS, **cfg.get("smtp", {})}
    cfg["archive"] = {"delete_after_send": False, **cfg.get("archive", {})}
    return cfg


def save_config():
    CONFIG_FILE.write_text(json.dumps(CFG, indent=2))


def recipients(s):
    return [a for a in re.split(r"[,;\s]+", s) if a]


# --- SOAP -------------------------------------------------------------------

class SoapFault(Exception):
    pass


def envelope(action, to, body, extra_headers=""):
    # Declare only the prefixes this message uses: the ET-4800 rejects requests
    # whose envelope declares a few unused namespaces ("OperationFailed").
    used = [p for p in ("wsd", "wse", "wsdp", "sca") if re.search(rf"\b{p}:", body + extra_headers)]
    xmlns = "".join(f' xmlns:{p}="{NS[p]}"' for p in ["soap", "wsa"] + used)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope{xmlns}>
<soap:Header>
<wsa:To>{esc(to)}</wsa:To>
<wsa:Action>{action}</wsa:Action>
<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>
<wsa:ReplyTo><wsa:Address>{ANONYMOUS}</wsa:Address></wsa:ReplyTo>
{extra_headers}
</soap:Header>
<soap:Body>{body}</soap:Body>
</soap:Envelope>"""


def text(el, path):
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else ""


def split_mtom(content_type, raw):
    """Split a multipart/related (MTOM/XOP) response into (xml, [attachments])."""
    m = re.search(r'boundary="?([^";]+)"?', content_type, re.I)
    if "multipart" not in content_type.lower() or not m:
        return raw, []
    boundary = b"--" + m.group(1).encode()
    parts = []
    for chunk in raw.split(boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        head, _, body = chunk.partition(b"\r\n\r\n")
        if body.endswith(b"\r\n"):
            body = body[:-2]
        parts.append((head.decode("latin-1").lower(), body))
    if not parts:
        return raw, []
    xml_index = next((i for i, (h, _) in enumerate(parts) if "xml" in h), 0)
    attachments = [b for i, (_, b) in enumerate(parts) if i != xml_index]
    return parts[xml_index][1], attachments


def soap_call(url, action, body, to=None, extra_headers="", timeout=180):
    data = envelope(action, to or url, body, extra_headers).encode()
    name = action.rsplit("/", 1)[-1]
    trace("→", f"{name}  {url}", data)
    req = urllib.request.Request(url, data, {"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype, raw = r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        ctype, raw = e.headers.get("Content-Type", ""), e.read()
    xml, attachments = split_mtom(ctype, raw)
    note = f"\n\n[+{len(attachments)} attachment(s), {sum(map(len, attachments))} bytes]" if attachments else ""
    trace("←", f"{name} response", xml.decode("utf-8", "replace") + note)
    if not xml.strip():
        return None, attachments
    root = ET.fromstring(xml)
    fault = root.find(".//soap:Fault", NS)
    if fault is not None:
        raise SoapFault(" | ".join(t.strip() for t in fault.itertext() if t.strip()))
    return root, attachments


def parse_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", s.strip())
    if not m:
        return None
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


# --- discovery --------------------------------------------------------------

def pick_address(addresses, prefer_ip=None):
    """Prefer the address containing the printer IP, then any IPv4 http URL."""
    prefer_ip = prefer_ip or printer_ip()
    for a in addresses:
        if prefer_ip and prefer_ip in a:
            return a
    for a in addresses:
        if re.match(r"https?://\d+\.\d+\.\d+\.\d+", a):
            return a
    return addresses[0]


def probe(target_ip=None, timeout=3, want=None, sweep=False):
    """WS-Discovery: probe by multicast (and unicast to target_ip, or with sweep=True
    to every host of the local /24, for networks where multicast replies get lost).
    Returns a list of {ip, epr, xaddr, types}; stops early once want(match) is true."""
    bodies = ["<wsd:Probe/>", "<wsd:Probe><wsd:Types>sca:ScanDeviceType</wsd:Types></wsd:Probe>"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.5)
    s.bind(("", 0))
    matches = {}
    try:
        targets = [(target_ip, 3702)] if target_ip else []
        targets.append(("239.255.255.250", 3702))
        for body in bodies:
            msg = envelope(A_PROBE, DISCOVERY_TO, body).encode()
            for t in targets:
                try:
                    s.sendto(msg, t)
                except OSError as e:
                    log(f"Probe to {t[0]} failed: {e}")
        if sweep:
            msg = envelope(A_PROBE, DISCOVERY_TO, bodies[0]).encode()
            me = local_ip()
            prefix = me.rsplit(".", 1)[0]
            for host in range(1, 255):
                ip = f"{prefix}.{host}"
                if ip != me:
                    try:
                        s.sendto(msg, (ip, 3702))
                    except OSError:
                        pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            for pm in root.iter(f"{{{NS['wsd']}}}ProbeMatch"):
                xaddrs = text(pm, "wsd:XAddrs").split()
                if not xaddrs:
                    continue
                m = {"ip": addr[0], "epr": text(pm, "wsa:EndpointReference/wsa:Address"),
                     "xaddr": pick_address(xaddrs, addr[0]), "types": text(pm, "wsd:Types")}
                key = m["epr"] or m["xaddr"]
                if key in matches:
                    continue
                matches[key] = m
                trace("←", f"ProbeMatch from {addr[0]}", data)
                if want and want(m):
                    return [m]
    finally:
        s.close()
    return list(matches.values())


def discover():
    """Find the selected printer (or, if none is selected, the first scanner). Returns (epr, xaddr)."""
    ip = printer_ip()
    if ip:
        def want(m):
            return m["ip"] == ip or ip in m["xaddr"]
    else:
        def want(m):
            return "ScanDeviceType" in m["types"]
    match = next((m for m in probe(ip or None, timeout=4, want=want) if want(m)), None)
    return (match["epr"], match["xaddr"]) if match else None


DEVICE_FIELDS = (("name", "FriendlyName"), ("manufacturer", "Manufacturer"), ("model", "ModelName"),
                 ("model_number", "ModelNumber"), ("firmware", "FirmwareVersion"),
                 ("serial", "SerialNumber"), ("web", "PresentationUrl"))


def device_metadata(url, epr=None, prefer_ip=None):
    """WS-Transfer Get on the device: returns (info dict, scanner service URL or None)."""
    root, _ = soap_call(url, A_GET, "", to=epr or url, timeout=10)
    info = {key: text(root, f".//wsdp:{tag}") for key, tag in DEVICE_FIELDS}
    for hosted in root.iter(f"{{{NS['wsdp']}}}Hosted"):
        if "ScannerServiceType" in text(hosted, "wsdp:Types"):
            addresses = [a.text.strip() for a in hosted.iterfind("wsa:EndpointReference/wsa:Address", NS) if a.text]
            if addresses:
                return info, pick_address(addresses, prefer_ip)
    return info, None


def find_scanners():
    """All WSD scanners on the network, with model information."""
    found = []
    for m in probe(timeout=4, sweep=True):
        if m["types"] and "ScanDeviceType" not in m["types"]:
            continue
        try:
            info, service = device_metadata(m["xaddr"], m["epr"], m["ip"])
        except Exception as e:
            info, service = {"name": f"(no answer: {e})"}, None
        if service or "ScanDeviceType" in m["types"]:
            found.append({**m, **info})
    return sorted(found, key=lambda m: m["ip"])


def local_ip():
    if ADVERTISE_IP:
        return ADVERTISE_IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((printer_ip() or "239.255.255.250", 3702))
        return s.getsockname()[0]
    finally:
        s.close()


def notify_url():
    return f"http://{local_ip()}:{WEB_PORT}/wsd"


# --- subscription -----------------------------------------------------------

class Scanner:
    def __init__(self):
        self.wake = threading.Event()
        self.restart = False
        self.device_url = self.device_epr = self.service_url = None
        self.manager_url = self.manager_id = None
        self.tokens = {}
        self.info = {}        # model, firmware, ... from the device metadata
        self.caps = None      # formats and per-source resolutions/colors/sizes
        self.status = {}      # scanner state and active conditions
        self.default_ticket = None
        self.polled_at = None
        self.lock = threading.Lock()
        self.active = False
        self.expires_at = 0
        self.renew_at = 0
        self.error = ""
        self.client_id = uuid.uuid4()

    def locate(self):
        with self.lock:
            if WSD_DEVICE_URL:
                self.device_url, self.device_epr = WSD_DEVICE_URL, None
            else:
                found = discover()
                if not found:
                    where = f" at {printer_ip()}" if printer_ip() else ""
                    raise RuntimeError(f"No WSD scanner answered the discovery probe{where}")
                self.device_epr, self.device_url = found
            log(f"Device: {self.device_url}")
            self.info, service = device_metadata(self.device_url, self.device_epr)
            if not service:
                raise RuntimeError("Device metadata lists no ScannerService (is WSD scanning enabled?)")
            self.service_url = service
            model = " ".join(v for v in (self.info.get("manufacturer"), self.info.get("model")) if v)
            log(f"Scanner service: {self.service_url}" + (f" ({model})" if model else ""))
            try:
                self.load_capabilities()
            except Exception as e:
                log(f"Could not read scanner capabilities, using built-in defaults: {e}")
            self.polled_at = now()

    def load_capabilities(self):
        root, _ = soap_call(self.service_url, A_GET_ELEMENTS,
                            "<sca:GetScannerElementsRequest><sca:RequestedElements>"
                            "<sca:Name>sca:ScannerDescription</sca:Name><sca:Name>sca:ScannerConfiguration</sca:Name>"
                            "<sca:Name>sca:ScannerStatus</sca:Name><sca:Name>sca:DefaultScanTicket</sca:Name>"
                            "</sca:RequestedElements></sca:GetScannerElementsRequest>", timeout=15)
        conf = root.find(".//sca:ScannerConfiguration", NS)
        if conf is None:
            raise RuntimeError("Scanner did not return its configuration")
        caps = {"formats": [e.text.strip() for e in conf.iterfind(".//sca:FormatValue", NS) if e.text],
                "content_types": [e.text.strip() for e in conf.iterfind(".//sca:ContentTypeValue", NS) if e.text]}
        for source, path, prefix in (("Platen", "sca:Platen", "Platen"), ("ADF", "sca:ADF/sca:ADFFront", "ADF")):
            node = conf.find(path, NS)
            if node is None:
                continue
            res = node.find(f"sca:{prefix}Resolutions", NS)
            size = node.find(f"sca:{prefix}MaximumSize", NS)
            caps[source] = {
                "dpi": sorted({int(w.text) for w in res.iterfind("sca:Widths/sca:Width", NS)}) if res is not None else [],
                "colors": [c.text.strip() for c in node.iterfind(f"sca:{prefix}Color/sca:ColorEntry", NS) if c.text],
                "max": (int(text(size, "sca:Width")), int(text(size, "sca:Height"))) if size is not None else None,
            }
        if "ADF" in caps:
            caps["ADF"]["duplex"] = text(conf, "sca:ADF/sca:ADFSupportsDuplex").lower() in ("true", "1")
        desc = root.find(".//sca:ScannerDescription", NS)
        if desc is not None:
            self.info["scanner_name"] = text(desc, "sca:ScannerName")
            self.info["location"] = text(desc, "sca:ScannerLocation")
        st = root.find(".//sca:ScannerStatus", NS)
        if st is not None:
            self.status = {
                "state": text(st, "sca:ScannerState"),
                "reasons": [e.text.strip() for e in st.iterfind(".//sca:ScannerStateReason", NS) if e.text],
                "conditions": [text(c, "sca:Name") for c in st.iterfind(".//sca:DeviceCondition", NS)],
            }
        self.caps = caps
        self.default_ticket = root.find(".//sca:DefaultScanTicket", NS)
        summary = "; ".join(f"{s}: {c['dpi']} dpi" for s, c in caps.items() if s in ("Platen", "ADF"))
        log(f"Scanner formats: {', '.join(caps['formats'])}; {summary}")

    def poll(self):
        """Re-read device info, capabilities and status (does not touch the subscription)."""
        self.locate()

    def _manager_header(self):
        return f"<wse:Identifier>{esc(self.manager_id)}</wse:Identifier>" if self.manager_id else ""

    def _set_expiry(self, root):
        ttl = parse_duration(text(root, ".//wse:Expires")) or 3600
        self.expires_at = time.time() + ttl
        self.renew_at = time.time() + min(RENEW_EVERY, ttl / 2)

    def subscribe(self):
        with CFG_LOCK:
            dests = list(CFG["destinations"])
        if not dests:
            self.error = "No destinations configured"
            return
        target = notify_url()
        entries = "".join(
            f"<sca:ScanDestination><sca:ClientDisplayName>{esc(d['name'])}</sca:ClientDisplayName>"
            f"<sca:ClientContext>{d['id']}</sca:ClientContext></sca:ScanDestination>" for d in dests)
        body = f"""<wse:Subscribe>
<wse:EndTo><wsa:Address>{target}</wsa:Address></wse:EndTo>
<wse:Delivery Mode="{WSE}/DeliveryModes/Push">
<wse:NotifyTo><wsa:Address>{target}</wsa:Address>
<wsa:ReferenceParameters><wse:Identifier>urn:uuid:{self.client_id}</wse:Identifier></wsa:ReferenceParameters>
</wse:NotifyTo>
</wse:Delivery>
<wse:Expires>{SUBSCRIPTION_TTL}</wse:Expires>
<wse:Filter Dialect="{NS['wsdp']}/Action">{A_SCAN_AVAILABLE}</wse:Filter>
<sca:ScanDestinations>{entries}</sca:ScanDestinations>
</wse:Subscribe>"""
        root, _ = soap_call(self.service_url, A_SUBSCRIBE, body, timeout=15)
        if root is None:
            raise RuntimeError("Empty Subscribe response")
        manager = root.find(".//wse:SubscriptionManager", NS)
        self.manager_url = text(manager, "wsa:Address") if manager is not None else self.service_url
        ident = manager.find(".//wse:Identifier", NS) if manager is not None else None
        self.manager_id = ident.text.strip() if ident is not None and ident.text else None
        self.tokens = {text(r, "sca:ClientContext"): text(r, "sca:DestinationToken")
                       for r in root.iter(f"{{{SCA}}}DestinationResponse")}
        self._set_expiry(root)
        self.active = True
        log(f"Subscribed with {len(dests)} destination(s); callbacks go to {target}")
        if not self.tokens:
            log("Warning: printer returned no destination tokens")

    def renew(self):
        root, _ = soap_call(self.manager_url, A_RENEW,
                            f"<wse:Renew><wse:Expires>{SUBSCRIPTION_TTL}</wse:Expires></wse:Renew>",
                            extra_headers=self._manager_header(), timeout=15)
        if root is not None:
            self._set_expiry(root)
        else:
            self.renew_at = time.time() + RENEW_EVERY

    def unsubscribe(self):
        if self.active and self.manager_url:
            try:
                soap_call(self.manager_url, A_UNSUBSCRIBE, "<wse:Unsubscribe/>",
                          extra_headers=self._manager_header(), timeout=10)
                log("Unsubscribed")
            except Exception as e:
                log(f"Unsubscribe failed: {e}")
        self.active = False

    def resubscribe(self):
        self.restart = True
        self.wake.set()

    def loop(self):
        while True:
            try:
                if self.restart:
                    self.restart = False
                    self.unsubscribe()
                if not self.service_url:
                    self.locate()
                if not self.active:
                    self.subscribe()
                elif time.time() >= self.renew_at:
                    self.renew()
                self.error = ""
                delay = 5
            except Exception as e:
                self.error = str(e)
                log(f"Subscription error: {e}")
                self.active = False
                self.service_url = None
                delay = 30
            self.wake.wait(delay)
            self.wake.clear()


SCANNER = Scanner()

# --- scanning ---------------------------------------------------------------

JOB_LOCK = threading.Lock()


def pick_format(formats, color):
    """Best output format for a color mode: JPEG for color/gray, TIFF for black & white,
    the device's PDF as a last resort."""
    jpeg = next((f for f in JPEG_FORMATS if f in formats), None)
    tiff = TIFF_FORMAT if TIFF_FORMAT in formats else None
    pdf = next((f for f in PDF_FORMATS if f in formats), None)
    order = (tiff, pdf, jpeg) if color == "BlackAndWhite1" else (jpeg, tiff, pdf)
    return next((f for f in order if f), None)


def scan_settings(source, d):
    """Fit the destination's settings to what the scanner reports it supports."""
    caps = SCANNER.caps or {}
    if source == "ADFDuplex" and caps.get("ADF") and not caps["ADF"].get("duplex"):
        log("Scanner has no duplex feeder; scanning one side")
        source = "ADF"
    src = caps.get("ADF" if source.startswith("ADF") else source, {})
    formats = caps.get("formats") or ["jfif"]

    colors = src.get("colors") or [d["color"]]
    color = d["color"]
    if color not in colors:
        color = next((c for c in ("Grayscale8", "RGB24") if c in colors), colors[0])
    fmt = pick_format(formats, color)
    if fmt is None:
        raise RuntimeError(f"Scanner offers no supported format (it has: {', '.join(formats)})")
    if color == "BlackAndWhite1" and fmt in JPEG_FORMATS:  # JPEG cannot hold 1-bit images
        color = "Grayscale8" if "Grayscale8" in colors else "RGB24"

    want = int(d["dpi"])
    dpis = src.get("dpi") or [want]
    dpi = min(dpis, key=lambda r: (abs(r - want), -r))
    w, h = PAPERS.get(d["paper"], PAPERS["A4"])
    if src.get("max"):
        w, h = min(w, src["max"][0]), min(h, src["max"][1])
    return {"source": source, "format": fmt, "dpi": dpi, "color": color, "w": w, "h": h}


def scan_ticket(source, d):
    """Build the ScanTicket XML. Returns (xml, settings actually used)."""
    s = scan_settings(source, d)
    source, w, h, dpi = s["source"], s["w"], s["h"], s["dpi"]
    images = 1 if source == "Platen" else 0
    duplex = source == "ADFDuplex"
    if SCANNER.default_ticket is None:
        side = f"""<sca:ScanRegion><sca:ScanRegionXOffset>0</sca:ScanRegionXOffset><sca:ScanRegionYOffset>0</sca:ScanRegionYOffset><sca:ScanRegionWidth>{w}</sca:ScanRegionWidth><sca:ScanRegionHeight>{h}</sca:ScanRegionHeight></sca:ScanRegion>
<sca:ColorProcessing>{s['color']}</sca:ColorProcessing>
<sca:Resolution><sca:Width>{dpi}</sca:Width><sca:Height>{dpi}</sca:Height></sca:Resolution>"""
        back = f"<sca:MediaBack>{side}</sca:MediaBack>" if duplex else ""
        return f"""<sca:ScanTicket>
<sca:JobDescription><sca:JobName>scan2mail</sca:JobName><sca:JobOriginatingUserName>scan2mail</sca:JobOriginatingUserName><sca:JobInformation>{esc(d['name'])}</sca:JobInformation></sca:JobDescription>
<sca:DocumentParameters>
<sca:Format>{s['format']}</sca:Format>
<sca:ImagesToTransfer>{images}</sca:ImagesToTransfer>
<sca:InputSource>{source}</sca:InputSource>
<sca:InputSize><sca:InputMediaSize><sca:Width>{w}</sca:Width><sca:Height>{h}</sca:Height></sca:InputMediaSize></sca:InputSize>
<sca:MediaSides><sca:MediaFront>{side}</sca:MediaFront>{back}</sca:MediaSides>
</sca:DocumentParameters>
</sca:ScanTicket>""", s

    # Start from the scanner's own default ticket so every element it expects is present.
    t = copy.deepcopy(SCANNER.default_ticket)
    t.tag = f"{{{SCA}}}ScanTicket"
    sides = t.find(".//sca:MediaSides", NS)
    if duplex and sides is not None and sides.find("sca:MediaBack", NS) is None:
        front = sides.find("sca:MediaFront", NS)
        if front is not None:
            back = copy.deepcopy(front)
            back.tag = f"{{{SCA}}}MediaBack"
            sides.append(back)

    def put(path, value):
        for el in t.iterfind(path, NS):
            el.text = str(value)

    put("sca:JobDescription/sca:JobName", "scan2mail")
    put("sca:JobDescription/sca:JobOriginatingUserName", "scan2mail")
    put("sca:JobDescription/sca:JobInformation", d["name"])
    put(".//sca:Format", s["format"])
    put(".//sca:ImagesToTransfer", images)
    put(".//sca:InputSource", source)
    put(".//sca:InputMediaSize/sca:Width", w)
    put(".//sca:InputMediaSize/sca:Height", h)
    put(".//sca:ScanRegionXOffset", 0)
    put(".//sca:ScanRegionYOffset", 0)
    put(".//sca:ScanRegionWidth", w)
    put(".//sca:ScanRegionHeight", h)
    put(".//sca:ColorProcessing", s["color"])
    put(".//sca:Resolution/sca:Width", dpi)
    put(".//sca:Resolution/sca:Height", dpi)
    return ET.tostring(t, encoding="unicode"), s


def scan(scan_id, token, source, d):
    """Create a scan job and fetch its pages. Returns (pages, dpi); pages are
    JPEG, TIFF or PDF bytes. Without scan_id/token this is a pull scan (for testing)."""
    svc = SCANNER.service_url
    ticket, s = scan_ticket(source, d)
    source, dpi = s["source"], s["dpi"]
    push = (f"<sca:ScanIdentifier>{esc(scan_id)}</sca:ScanIdentifier>"
            f"<sca:DestinationToken>{esc(token)}</sca:DestinationToken>") if scan_id else ""
    body = f"<sca:CreateScanJobRequest>{push}{ticket}</sca:CreateScanJobRequest>"
    root, _ = soap_call(svc, A_CREATE_JOB, body, timeout=30)
    job_id, job_token = text(root, ".//sca:JobId"), text(root, ".//sca:JobToken")
    log(f"Job {job_id} created ({source}, {s['format']}, {s['color']}, {dpi} dpi)")
    pages = []
    try:
        while True:
            req = (f"<sca:RetrieveImageRequest><sca:JobId>{esc(job_id)}</sca:JobId>"
                   f"<sca:JobToken>{esc(job_token)}</sca:JobToken><sca:DocumentDescription>"
                   f"<sca:DocumentName>page{len(pages) + 1}</sca:DocumentName>"
                   f"</sca:DocumentDescription></sca:RetrieveImageRequest>")
            try:
                _, attachments = soap_call(svc, A_RETRIEVE, req)
            except SoapFault as e:
                if pages and "NoImagesAvailable" in str(e):
                    break
                raise
            if not attachments:
                break
            pages.append(attachments[0])
            log(f"Job {job_id}: page {len(pages)} received ({len(attachments[0]) // 1024} KB)")
            if source == "Platen":
                break
    except Exception:
        try:
            soap_call(svc, A_CANCEL, f"<sca:CancelJobRequest><sca:JobId>{esc(job_id)}</sca:JobId></sca:CancelJobRequest>", timeout=10)
        except Exception:
            pass
        raise
    return pages, dpi


def run_job(context, scan_id, event_source):
    with CFG_LOCK:
        d = next((x for x in CFG["destinations"] if x["id"] == context), None)
    if d is None:
        log(f"Scan event for unknown destination {context!r}; ignoring")
        return
    job = {"time": now(), "dest": d["name"], "status": "scanning", "files": []}
    JOBS.appendleft(job)
    with JOB_LOCK:
        try:
            if d["source"] != "auto":
                sources = [d["source"]]
            elif event_source:
                sources = [event_source]
            else:
                sources = ["ADF", "Platen"]
            pages, dpi = [], int(d["dpi"])
            for i, source in enumerate(sources):
                try:
                    pages, dpi = scan(scan_id, SCANNER.tokens.get(context, ""), source, d)
                    if pages:
                        break
                except SoapFault as e:
                    log(f"{source} scan failed: {e}")
                    if i == len(sources) - 1:
                        raise
            if not pages:
                raise RuntimeError("Scanner returned no pages")

            docs = build_documents(pages, dpi)
            slug = re.sub(r"[^A-Za-z0-9]+", "-", d["name"]).strip("-") or "scan"
            base = f"{datetime.now():%Y-%m-%d_%H%M%S}_{slug}"
            names = [f"{base}.pdf"] if len(docs) == 1 else [f"{base}_{i}.pdf" for i in range(1, len(docs) + 1)]
            for doc, name in zip(docs, names):
                (SCAN_DIR / name).write_bytes(doc)
            job["files"] = names
            log(f"Saved {', '.join(names)} ({len(pages)} page(s))")

            to = recipients(d["to"])
            with CFG_LOCK:
                smtp_ready = bool(CFG["smtp"]["host"])
            if smtp_ready and to:
                send_mail(to, f"{CFG['smtp']['subject']}: {names[0]}",
                          f"Scanned document attached ({len(pages)} page(s)).", list(zip(docs, names)))
                job["status"] = f"emailed to {', '.join(to)}"
                log(f"Emailed {', '.join(names)} to {', '.join(to)}")
                if CFG["archive"]["delete_after_send"]:
                    for name in names:
                        (SCAN_DIR / name).unlink(missing_ok=True)
                    job["status"] += "; removed from archive"
                    log(f"Deleted {', '.join(names)} after sending")
            else:
                job["status"] = "saved (SMTP or recipients not configured)"
        except Exception as e:
            job["status"] = f"failed: {e}"
            log(f"Scan job failed: {e}")
            traceback.print_exc()


def handle_notification(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        log("Unparseable notification")
        return
    action = text(root, "soap:Header/wsa:Action")
    if action == A_SCAN_AVAILABLE:
        ev = root.find(".//sca:ScanAvailableEvent", NS)
        context, scan_id = text(ev, "sca:ClientContext"), text(ev, "sca:ScanIdentifier")
        source = text(ev, ".//sca:InputSource")
        log(f"Scan button pressed for destination {context}")
        threading.Thread(target=run_job, args=(context, scan_id, source), daemon=True).start()
    elif action == A_SUBSCRIPTION_END:
        log("Printer ended the subscription; resubscribing")
        SCANNER.active = False
        SCANNER.wake.set()
    else:
        log(f"Ignoring notification {action}")


# --- PDF --------------------------------------------------------------------

def jpeg_info(data):
    """Return (width, height, components, dpi or None) from a JPEG."""
    dpi = None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE0 and data[i + 4:i + 9] == b"JFIF\0" and data[i + 11] == 1:
            dpi = struct.unpack(">H", data[i + 12:i + 14])[0] or None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h, data[i + 9], dpi
        i += 2 + seglen
    raise ValueError("Scanner did not return a JPEG image")


def tiff_image(data):
    """Decode an uncompressed single-page TIFF.
    Returns (width, height, bits, components, photometric, raw samples, dpi or None)."""
    bo = {b"II": "<", b"MM": ">"}.get(data[:2])
    if not bo or struct.unpack(bo + "H", data[2:4])[0] != 42:
        raise ValueError("Scanner returned an image that is neither JPEG nor TIFF")
    ifd = struct.unpack(bo + "I", data[4:8])[0]
    count = struct.unpack(bo + "H", data[ifd:ifd + 2])[0]
    sizes = {3: 2, 4: 4, 5: 8}
    tags = {}
    for k in range(count):
        e = ifd + 2 + 12 * k
        tag, typ, n = struct.unpack(bo + "HHI", data[e:e + 8])
        if typ not in sizes:
            continue
        size = sizes[typ] * n
        off = e + 8 if size <= 4 else struct.unpack(bo + "I", data[e + 8:e + 12])[0]
        if typ == 3:
            tags[tag] = struct.unpack(bo + "%dH" % n, data[off:off + size])
        elif typ == 4:
            tags[tag] = struct.unpack(bo + "%dI" % n, data[off:off + size])
        else:
            v = struct.unpack(bo + "%dI" % (2 * n), data[off:off + size])
            tags[tag] = tuple(v[i] / v[i + 1] if v[i + 1] else 0 for i in range(0, len(v), 2))
    if tags.get(259, (1,))[0] != 1:
        raise ValueError("Compressed TIFF is not supported")
    width, height = tags[256][0], tags[257][0]
    bits, comps = tags.get(258, (1,))[0], tags.get(277, (1,))[0]
    offsets = tags[273]
    lengths = tags.get(279) or (len(data) - offsets[0],)
    raw = b"".join(data[o:o + c] for o, c in zip(offsets, lengths))
    dpi = None
    if 282 in tags:
        unit = tags.get(296, (2,))[0]
        dpi = tags[282][0] if unit == 2 else tags[282][0] * 2.54 if unit == 3 else None
    return width, height, bits, comps, tags.get(262, (1,))[0], raw, dpi


def image_xobject(page, default_dpi):
    """PDF image object for a JPEG or uncompressed TIFF page. Returns (object bytes, w, h, dpi)."""
    if page[:2] == b"\xff\xd8":
        w, h, comps, dpi = jpeg_info(page)
        cs = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}[comps]
        obj = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
               b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
               % (w, h, cs, len(page)) + page + b"\nendstream")
    else:
        w, h, bits, comps, photometric, raw, dpi = tiff_image(page)
        if comps not in (1, 3):
            raise ValueError(f"TIFF with {comps} channels is not supported")
        cs = b"/DeviceGray" if comps == 1 else b"/DeviceRGB"
        decode = b" /Decode [1 0]" if photometric == 0 else b""  # 0 = WhiteIsZero
        data = zlib.compress(raw[:((w * bits * comps + 7) // 8) * h])
        obj = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
               b"/BitsPerComponent %d%s /Filter /FlateDecode /Length %d >>\nstream\n"
               % (w, h, cs, bits, decode, len(data)) + data + b"\nendstream")
    dpi = dpi if dpi and dpi > 10 else default_dpi
    return obj, w, h, dpi


def build_documents(pages, dpi):
    """Image pages become one PDF; PDFs produced by the scanner itself are kept as they are."""
    images = [p for p in pages if p[:4] != b"%PDF"]
    pdfs = [p for p in pages if p[:4] == b"%PDF"]
    return ([make_pdf(images, dpi)] if images else []) + pdfs


def make_pdf(pages, default_dpi):
    objs = [None, None]  # 1 = catalog, 2 = page tree

    def add(b):
        objs.append(b)
        return len(objs)

    kids = []
    for p in pages:
        obj, w, h, dpi = image_xobject(p, default_dpi)
        img = add(obj)
        pw, ph = w * 72 / dpi, h * 72 / dpi
        content = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im0 Do Q".encode()
        cont = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] "
                        f"/Resources << /XObject << /Im0 {img} 0 R >> >> /Contents {cont} 0 R >>".encode()))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, b in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + b + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offsets:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


# --- email ------------------------------------------------------------------

def send_mail(to, subject, body, attachments=()):
    """attachments: list of (pdf bytes, filename)."""
    with CFG_LOCK:
        s = dict(CFG["smtp"])
    if not s["host"]:
        raise RuntimeError("SMTP server is not configured")
    if not to:
        raise RuntimeError("No recipients")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["from"] or s["user"]
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    for data, filename in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    port = int(s["port"] or 0)
    if s["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(s["host"], port or 465, timeout=60)
    else:
        smtp = smtplib.SMTP(s["host"], port or 587, timeout=60)
    with smtp:
        if s["security"] == "starttls":
            smtp.starttls()
        if s["user"]:
            smtp.login(s["user"], s["password"])
        smtp.send_message(msg)


# --- web UI -----------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#667085;--line:#e3e6eb;--accent:#2f6fde;--ok:#1a7f4b;--bad:#c23a3a}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2129;--fg:#e6e8ec;--muted:#98a1b0;--line:#2d333d;--accent:#6b9bf2;--ok:#4cc38a;--bad:#f07171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
input,select{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 7px}
input[type=text]{width:100%;min-width:120px}button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:6px;padding:5px 10px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--bad)}
.dest{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;align-items:end;padding:12px 0;border-bottom:1px solid var(--line)}
.dest label{display:flex;flex-direction:column;gap:3px;color:var(--muted);font-size:12px}.dest .wide{grid-column:span 2}
.actions{display:flex;gap:6px;flex-wrap:wrap}.ok{color:var(--ok)}.bad{color:var(--bad)}.muted{color:var(--muted)}
button:disabled{opacity:.5;cursor:default}
pre{white-space:pre-wrap;word-break:break-all;font:12px/1.4 ui-monospace,monospace;margin:0}a{color:var(--accent)}
details{border-bottom:1px solid var(--line);padding:6px 0}summary{cursor:pointer}
"""


def page(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"""


def options(choices, selected):
    items = choices.items() if isinstance(choices, dict) else ((c, c) for c in choices)
    return "".join(f'<option value="{k}"{" selected" if k == selected else ""}>{html.escape(v)}</option>' for k, v in items)


def dpi_choices():
    """Resolutions the scanner reports, or a generic list before it has been reached."""
    caps = SCANNER.caps or {}
    found = sorted({r for s in ("Platen", "ADF") for r in caps.get(s, {}).get("dpi", [])})
    return [str(r) for r in found] or DPIS


def source_choices():
    caps = SCANNER.caps
    if not caps:
        return SOURCES
    keep = set()
    if "Platen" in caps:
        keep.add("Platen")
    if "ADF" in caps:
        keep.add("ADF")
        if caps["ADF"].get("duplex"):
            keep.add("ADFDuplex")
    if {"Platen", "ADF"} <= keep:
        keep.add("auto")
    return {k: v for k, v in SOURCES.items() if k in keep} or SOURCES


def color_choices():
    caps = SCANNER.caps
    if not caps:
        return COLORS
    colors = {c for s in ("Platen", "ADF") for c in caps.get(s, {}).get("colors", [])}
    formats = caps.get("formats", [])
    if TIFF_FORMAT not in formats and not any(f in formats for f in PDF_FORMATS):
        colors.discard("BlackAndWhite1")  # would need JPEG, which has no 1-bit mode
    return {k: v for k, v in COLORS.items() if k in colors} or COLORS


def with_current(choices, current):
    """Keep a saved value selectable even if the current printer doesn't offer it."""
    if isinstance(choices, dict):
        return choices if current in choices else {**choices, current: f"{current} (not supported)"}
    return choices if current in choices else choices + [current]


def dest_form(d):
    new = d is None
    d = d or new_destination(name="", to="")
    e = html.escape
    buttons = ('<button class="primary">Add</button>' if new else
               '<button class="primary">Save</button>'
               '<button formaction="/dest/test">Test email</button>'
               '<button class="danger" formaction="/dest/delete">Delete</button>')
    return f"""<form method="post" action="/dest/save" class="dest">
<input type="hidden" name="id" value="{'' if new else e(d['id'])}">
<label>Name on printer<input type="text" name="name" maxlength="32" required value="{e(d['name'])}"></label>
<label class="wide">Email to (comma separated)<input type="text" name="to" required value="{e(d['to'])}"></label>
<label>Source<select name="source">{options(with_current(source_choices(), d['source']), d['source'])}</select></label>
<label>Color<select name="color">{options(with_current(color_choices(), d['color']), d['color'])}</select></label>
<label>DPI<select name="dpi">{options(with_current(dpi_choices(), d['dpi']), d['dpi'])}</select></label>
<label>Paper<select name="paper">{options(list(PAPERS), d['paper'])}</select></label>
<div class="actions">{buttons}</div></form>"""


def smtp_form(s):
    e = html.escape
    pw_hint = "leave blank to keep current" if s["password"] else ""
    return f"""<form method="post" action="/smtp/save" class="dest">
<label class="wide">Server<input type="text" name="host" value="{e(s['host'])}" placeholder="smtp.example.com"></label>
<label>Port<input type="text" name="port" value="{e(str(s['port']))}"></label>
<label class="wide">Encryption<select name="security">{options(SECURITY, s['security'])}</select></label>
<label class="wide">Username<input type="text" name="user" value="{e(s['user'])}" autocomplete="off"></label>
<label class="wide">Password<input type="password" name="password" placeholder="{pw_hint}" autocomplete="new-password"></label>
<label class="wide">From address<input type="text" name="from" value="{e(s['from'])}"></label>
<label class="wide">Subject prefix<input type="text" name="subject" value="{e(s['subject'])}"></label>
<label class="wide">Send test to<input type="text" name="test_to" placeholder="you@example.com"></label>
<div class="actions"><button class="primary">Save</button><button formaction="/smtp/test">Save &amp; send test</button></div>
</form>"""


DISCOVERED = []  # result of the last "Find scanners" click


def printer_card():
    e = html.escape
    s = SCANNER
    i = s.info or {}
    if i:
        web = f'<a href="{e(i["web"])}" target="_blank" rel="noopener">{e(i["web"])}</a>' if i.get("web") else "—"
        state = s.status.get("state", "—") if s.status else "—"
        extra = s.status.get("conditions", []) + s.status.get("reasons", []) if s.status else []
        if extra:
            state += " (" + ", ".join(extra) + ")"
        rows = [("Name", i.get("name") or i.get("scanner_name")), ("Manufacturer", i.get("manufacturer")),
                ("Model", " / ".join(dict.fromkeys(v for v in (i.get("model"), i.get("model_number")) if v))),
                ("Firmware", i.get("firmware")), ("Serial number", i.get("serial")),
                ("Location", i.get("location")), ("Scanner state", state)]
        info = "".join(f"<tr><th>{k}</th><td>{e(v or '—')}</td></tr>" for k, v in rows)
        info += f"<tr><th>Printer web page</th><td>{web}</td></tr>"
        info += f"<tr><th>Last polled</th><td>{e(s.polled_at or '—')}</td></tr>"
    else:
        info = '<tr><td>Not connected yet. Use <b>Poll features</b> or <b>Find scanners</b>.</td></tr>'

    caps_html = ""
    if s.caps:
        def mm(v):
            return f"{v[0] * 0.0254:.0f} × {v[1] * 0.0254:.0f} mm" if v else "—"

        def color_names(cs):
            return ", ".join(COLORS.get(c, c) for c in cs) or "—"
        rows = ""
        for src, label in (("Platen", "Glass"), ("ADF", "Document feeder")):
            c = s.caps.get(src)
            if c:
                duplex = ("yes" if c.get("duplex") else "no") if src == "ADF" else "—"
                rows += (f"<tr><td>{label}</td><td>{e(', '.join(map(str, c['dpi'])) or '—')}</td>"
                         f"<td>{e(color_names(c['colors']))}</td><td>{e(mm(c['max']))}</td><td>{duplex}</td></tr>")
        caps_html = f"""<h2 style="margin-top:16px">Scanner features</h2>
<table><tr><th>Output formats</th><td colspan="4">{e(', '.join(s.caps['formats']) or '—')}</td></tr>
<tr><th>Source</th><th>Resolutions (dpi)</th><th>Color modes</th><th>Max. size</th><th>Both sides</th></tr>{rows}</table>"""

    found = ""
    if DISCOVERED:
        items = "".join(
            f"<tr><td>{e(m.get('name') or '—')}</td><td>{e(' '.join(v for v in (m.get('manufacturer'), m.get('model')) if v) or '—')}</td>"
            f"<td>{e(m['ip'])}</td><td><form method=\"post\" action=\"/printer/select\"><input type=\"hidden\" name=\"ip\" value=\"{e(m['ip'])}\">"
            f"<button{' disabled' if m['ip'] == printer_ip() else ''}>{'Selected' if m['ip'] == printer_ip() else 'Use this printer'}</button></form></td></tr>"
            for m in DISCOVERED)
        found = f'<h2 style="margin-top:16px">Scanners on the network</h2><table><tr><th>Name</th><th>Model</th><th>IP</th><th></th></tr>{items}</table>'

    return f"""<table>{info}</table>{caps_html}
<div class="actions" style="margin-top:12px">
<form method="post" action="/printer/poll"><button class="primary">Poll features</button></form>
<form method="post" action="/printer/discover"><button>Find scanners</button></form>
<form method="post" action="/printer/select" class="actions"><input type="text" name="ip" placeholder="Printer IP" value="{e(printer_ip())}" style="width:150px"><button>Use IP</button></form>
</div>{found}"""


def archive_card(delete_after_send):
    e = html.escape
    files = sorted(SCAN_DIR.glob("*.pdf"), reverse=True)
    rows = "".join(
        f'<tr><td><a href="/scans/{quote(f.name)}">{e(f.name)}</a></td><td>{f.stat().st_size / 1024:,.0f} KB</td>'
        f'<td><form method="post" action="/archive/delete"><input type="hidden" name="name" value="{e(f.name)}">'
        f'<button class="danger">Delete</button></form></td></tr>'
        for f in files[:100]) or '<tr><td colspan="3">Empty</td></tr>'
    more = f'<p class="sub">Showing 100 of {len(files)} files.</p>' if len(files) > 100 else ""
    checked = " checked" if delete_after_send else ""
    total = sum(f.stat().st_size for f in files) / 1024 / 1024
    return f"""<form method="post" action="/archive/settings" class="actions" style="align-items:center;margin-bottom:12px">
<label><input type="checkbox" name="delete_after_send" value="1"{checked} onchange="this.form.submit()"> Delete scans after they were emailed successfully</label>
<noscript><button>Save</button></noscript></form>
<table><tr><th>File</th><th>Size</th><th></th></tr>{rows}</table>{more}
<div class="actions" style="margin-top:12px;align-items:center"><span class="muted">{len(files)} file(s), {total:.1f} MB</span>
<form method="post" action="/archive/delete-all" onsubmit="return confirm('Delete all {len(files)} scans from the archive?')">
<button class="danger"{' disabled' if not files else ''}>Delete all</button></form></div>"""


def render_index(flash=""):
    e = html.escape
    s = SCANNER
    if s.active:
        sub = f'<span class="ok">active</span>, expires {datetime.fromtimestamp(s.expires_at):%H:%M:%S}'
    else:
        sub = '<span class="bad">not active</span>'
    with CFG_LOCK:
        smtp = dict(CFG["smtp"])
    mail = (e(f"{smtp['host']}:{smtp['port']} from {smtp['from'] or smtp['user']}") if smtp["host"]
            else '<span class="bad">SMTP server not configured</span>')
    rows = [("Printer", e(printer_ip() or "auto-discover")), ("Device", e(s.device_url or "—")),
            ("Scanner service", e(s.service_url or "—")), ("Callback URL", e(notify_url())),
            ("Subscription", sub), ("Email", mail)]
    if s.error:
        rows.append(("Last error", f'<span class="bad">{e(s.error)}</span>'))
    status = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

    with CFG_LOCK:
        dests = "".join(dest_form(d) for d in CFG["destinations"])

    def file_links(names):
        return "<br>".join(f'<a href="/scans/{quote(n)}">{e(n)}</a>' if (SCAN_DIR / n).is_file()
                           else f'<span class="muted">{e(n)} (deleted)</span>' for n in names)

    jobs = "".join(
        f"<tr><td>{e(j['time'])}</td><td>{e(j['dest'])}</td><td>{e(j['status'])}</td>"
        f"<td>{file_links(j['files'])}</td></tr>"
        for j in JOBS) or '<tr><td colspan="4">No scans yet.</td></tr>'
    with CFG_LOCK:
        delete_after_send = CFG["archive"]["delete_after_send"]
    archive = archive_card(delete_after_send)
    logs = e("\n".join(reversed(LOG))) or "Nothing logged yet."
    flash_html = f'<div class="card">{e(flash)}</div>' if flash else ""

    return page("Scan to Email", f"""
<h1>Scan to Email</h1>
<p class="sub">On the printer: Scan &rarr; Computer (WSD) &rarr; pick a destination below.</p>
{flash_html}
<div class="card"><h2>Status</h2><table>{status}</table>
<form method="post" action="/resubscribe" style="margin-top:10px"><button>Reconnect to printer</button>
<a href="/debug" style="margin-left:10px">SOAP traffic</a></form></div>
<div class="card"><h2>Printer</h2>{printer_card()}</div>
<div class="card"><h2>Destinations</h2>
<p class="sub">Each destination shows up on the printer as its own WSD computer.</p>
{dests}<h2 style="margin-top:16px">Add destination</h2>{dest_form(None)}</div>
<div class="card"><h2>Mail server</h2>{smtp_form(smtp)}</div>
<div class="card"><h2>Recent scans</h2><table><tr><th>Time</th><th>Destination</th><th>Status</th><th>File</th></tr>{jobs}</table></div>
<div class="card"><h2>Archive</h2>{archive}</div>
<div class="card"><h2>Log</h2><pre>{logs}</pre></div>""")


def render_debug():
    e = html.escape
    items = "".join(f"<details{' open' if i == 0 else ''}><summary>{e(t)} {e(d)} {e(w)}</summary><pre>{e(x)}</pre></details>"
                    for i, (t, d, w, x) in enumerate(reversed(TRAFFIC))) or "<p>No traffic yet.</p>"
    return page("SOAP Traffic", f'<h1>SOAP traffic</h1><p class="sub"><a href="/">&larr; back</a> · newest first, last {TRAFFIC.maxlen} messages</p><div class="card">{items}</div>')


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def respond(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self):
        self.respond(303, headers=[("Location", "/")])

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.respond(200, render_index())
        elif path == "/debug":
            self.respond(200, render_debug())
        elif path.startswith("/scans/"):
            name = path[len("/scans/"):]
            f = SCAN_DIR / name
            if re.fullmatch(r"[\w.-]+\.pdf", name) and f.is_file():
                self.respond(200, f.read_bytes(), "application/pdf",
                             [("Content-Disposition", f'inline; filename="{name}"')])
            else:
                self.respond(404, "Not found")
        else:
            self.respond(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self.read_body()
        if path == "/wsd":
            trace("←", f"notification from {self.client_address[0]}", raw)
            self.respond(202, ctype="application/soap+xml")
            handle_notification(raw)
            return

        form = {k: v[0].strip() for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if path == "/dest/save":
            self.save_destination(form)
        elif path == "/dest/delete":
            with CFG_LOCK:
                CFG["destinations"] = [d for d in CFG["destinations"] if d["id"] != form.get("id")]
                save_config()
            SCANNER.resubscribe()
            self.redirect()
        elif path == "/dest/test":
            self.send_test(form.get("to", ""), form.get("name", ""))
        elif path in ("/smtp/save", "/smtp/test"):
            with CFG_LOCK:
                s = CFG["smtp"]
                for key in ("host", "port", "user", "from", "subject"):
                    s[key] = form.get(key, "")
                s["security"] = form.get("security") if form.get("security") in SECURITY else "starttls"
                if form.get("password"):
                    s["password"] = form["password"]
                save_config()
            log("Mail server settings saved")
            if path == "/smtp/test":
                self.send_test(form.get("test_to", ""), "mail server test")
            else:
                self.redirect()
        elif path == "/resubscribe":
            SCANNER.service_url = None  # rediscover too
            SCANNER.resubscribe()
            self.redirect()
        elif path == "/archive/delete":
            name = form.get("name", "")
            if re.fullmatch(r"[\w.-]+\.pdf", name) and (SCAN_DIR / name).is_file():
                (SCAN_DIR / name).unlink()
                log(f"Deleted {name} from archive")
            self.redirect()
        elif path == "/archive/delete-all":
            files = list(SCAN_DIR.glob("*.pdf"))
            for f in files:
                f.unlink(missing_ok=True)
            log(f"Deleted all {len(files)} scan(s) from archive")
            self.redirect()
        elif path == "/archive/settings":
            with CFG_LOCK:
                CFG["archive"]["delete_after_send"] = form.get("delete_after_send") == "1"
                save_config()
            self.redirect()
        elif path == "/printer/poll":
            try:
                SCANNER.poll()
                i = SCANNER.info
                flash = f"Read features of {i.get('manufacturer', '')} {i.get('model') or i.get('name') or 'the printer'}."
            except Exception as e:
                flash = f"Polling the printer failed: {e}"
            self.respond(200, render_index(flash))
        elif path == "/printer/discover":
            global DISCOVERED
            try:
                DISCOVERED = find_scanners()
                flash = f"Found {len(DISCOVERED)} scanner(s)." if DISCOVERED else \
                    "No WSD scanners answered (multicast needs host networking)."
            except Exception as e:
                flash = f"Discovery failed: {e}"
            self.respond(200, render_index(flash))
        elif path == "/printer/select":
            ip = form.get("ip", "")
            if ip and not re.fullmatch(r"[\w.:-]+", ip):
                self.respond(200, render_index("That is not a valid IP address or host name."))
                return
            with CFG_LOCK:
                CFG["printer_ip"] = ip
                save_config()
            log(f"Printer set to {ip or 'auto-discover'}")
            SCANNER.info, SCANNER.caps, SCANNER.status, SCANNER.default_ticket = {}, None, {}, None
            SCANNER.service_url = None
            SCANNER.resubscribe()
            self.redirect()
        else:
            self.respond(404, "Not found")

    def send_test(self, to, label):
        try:
            send_mail(recipients(to), f"{CFG['smtp']['subject']}: test",
                      f"Test message from the scan-to-email service ({label}).")
            self.respond(200, render_index(f"Test email sent to {to}."))
        except Exception as e:
            self.respond(200, render_index(f"Test email failed: {e}"))

    def save_destination(self, form):
        d = {"name": form.get("name", "")[:32],
             "to": form.get("to", ""),
             "source": form.get("source") if form.get("source") in SOURCES else "auto",
             "color": form.get("color") if form.get("color") in COLORS else "RGB24",
             "dpi": form.get("dpi") if form.get("dpi", "").isdigit() else "300",
             "paper": form.get("paper") if form.get("paper") in PAPERS else "A4"}
        if not d["name"] or not recipients(d["to"]):
            self.respond(200, render_index("Name and at least one email address are required."))
            return
        with CFG_LOCK:
            existing = next((x for x in CFG["destinations"] if x["id"] == form.get("id")), None)
            if existing:
                renamed = existing["name"] != d["name"]
                existing.update(d)
            else:
                renamed = True
                CFG["destinations"].append({"id": uuid.uuid4().hex[:8], **d})
            save_config()
        if renamed:  # only names live on the printer; other settings apply at scan time
            SCANNER.resubscribe()
        self.redirect()


# --- main -------------------------------------------------------------------

def shutdown(*_):
    log("Shutting down")
    SCANNER.unsubscribe()
    sys.exit(0)


def main():
    global CFG
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    CFG = load_config()
    save_config()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=SCANNER.loop, daemon=True).start()
    log(f"Web UI on http://{local_ip()}:{WEB_PORT}/")
    ThreadingHTTPServer(("", WEB_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
