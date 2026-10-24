# Artnet2KNX

Gateway, das Art-Net (DMX über UDP) empfängt und daraus KNX-Telegramme macht.
Läuft als Dienst auf einem Server (Linux, Python ≥ 3.10).

Unterstützt:

| Typ      | KNX-Datenpunkt          | Verhalten                                                     |
|----------|-------------------------|---------------------------------------------------------------|
| `switch` | DPT 1.001 (1 Bit)       | EIN, wenn DMX-Wert ≥ `threshold` (Standard 128), sonst AUS    |
| `dim`    | DPT 5.001 (1 Byte)      | DMX 0–255 wird 1:1 als Byte gesendet (= 0–100 %)              |

- KNX über **IP Routing** (Multicast, Standard `224.0.23.12:3671`) oder **Tunneling** (UDP/TCP) – umgesetzt mit [xknx](https://xknx.github.io/)
- Es werden nur **Änderungen** gesendet.
- **Drosselung** pro Gruppenadresse (`min_interval`): Art-Net kommt mit bis zu 44 Hz, beim Faden würde das den Bus fluten. Die erste Änderung geht sofort raus, danach höchstens ein Telegramm pro Intervall, und der letzte Wert wird immer nachgesendet.
- Zusätzlich gibt es ein globales Limit für Telegramme pro Sekunde (`knx.rate_limit`).
- Antwortet auf **ArtPoll**, damit Lichtpulte den Gateway als Node finden.
- Ein DMX-Kanal kann auf mehrere Gruppenadressen gehen (z. B. Dimmwert + Schalten).

## Installation

```bash
git clone https://github.com/msfuture/Artnet2KNX.git /opt/artnet2knx
cd /opt/artnet2knx
python3 -m venv venv
venv/bin/pip install .
```

## Konfiguration

Es gibt zwei Dateien (Beispiele in `config/`):

### `config.yaml` – Grundeinstellungen

```yaml
knx:
  connection_type: routing        # routing | tunneling | tunneling_tcp | automatic
  multicast_group: 224.0.23.12    # KNX IP Routing Multicast-Adresse
  multicast_port: 3671
  individual_address: 15.15.250   # Physikalische Adresse des Gateways
  local_ip: null                  # Netzwerkkarte für KNX (null = automatisch)
  gateway_ip: null                # nur Tunneling
  rate_limit: 20                  # Telegramme/s

artnet:
  bind_ip: 0.0.0.0
  port: 6454
  poll_reply: true

defaults:
  switch_threshold: 128
  switch_min_interval: 0.0
  dim_min_interval: 0.2

mapping_file: mapping.yaml        # relativ zur config.yaml
```

Alle Optionen sind in [`config/config.yaml`](config/config.yaml) kommentiert.

### `mapping.yaml` – Universen und Kanäle

```yaml
universes:
  - universe: 0                  # Port-Address 0..32767 oder "Net:SubNet:Universe"
    channels:
      - channel: 1               # DMX-Kanal 1..512
        type: switch
        group_address: 1/0/1
        name: Wohnzimmer Decke   # optional
        threshold: 128           # optional (nur switch)

      - channel: 2
        type: dim
        group_address: 1/1/2
        min_interval: 0.2        # optional
        invert: false            # optional, 255 - Wert
```

> **Hinweis zur Universumsnummer:** Art-Net zählt ab 0. Manche Pulte zeigen
> „Universe 1“ an und senden dabei Art-Net-Universum 0. Mit `-v` siehst du im
> Log, welche Universen ankommen, für die es kein Mapping gibt.

## Starten

```bash
# Konfiguration prüfen und Mapping anzeigen
artnet2knx -c config/config.yaml --check

# Testlauf ohne KNX-Verbindung (Telegramme werden nur geloggt)
artnet2knx -c config/config.yaml --dry-run -v

# Normalbetrieb
artnet2knx -c config/config.yaml
```

Alternativ ohne Installation: `python -m artnet2knx -c config/config.yaml`.

### Als systemd-Dienst

```bash
sudo useradd --system --no-create-home artnet2knx
sudo mkdir -p /etc/artnet2knx
sudo cp config/config.yaml config/mapping.yaml /etc/artnet2knx/
sudo cp systemd/artnet2knx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now artnet2knx
journalctl -u artnet2knx -f
```

Firewall: UDP 6454 (Art-Net) eingehend und UDP 3671 / Multicast `224.0.23.12` (KNX) freigeben.

## Tests

```bash
pip install -e .[test]
pytest
```

## Aufbau

```
artnet2knx/
  artnet.py    Art-Net Empfang (ArtDmx, ArtPoll/ArtPollReply)
  config.py    Laden und Prüfen der YAML-Dateien
  gateway.py   DMX -> KNX Umrechnung, Änderungserkennung, Drosselung
  knx.py       KNX-Verbindung und Telegramme (xknx)
  __main__.py  Kommandozeile / Dienst
```
