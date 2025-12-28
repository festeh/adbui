# adbui

Terminal UI for managing Android devices via ADB with mDNS discovery.

## Features

- Auto-discover devices via mDNS (no manual IP entry)
- Pair, connect, disconnect devices
- Real-time device status updates
- Vim-style navigation (j/k)

## Install

```bash
uv tool install .
```

## Usage

```bash
adbui
```

### Keybindings

| Key | Action |
|-----|--------|
| `r` | Refresh |
| `c` | Connect |
| `p` | Pair |
| `d` | Disconnect |
| `K` | Restart ADB server |
| `l` | Toggle logs |
| `j/k` | Navigate |
| `q` | Quit |

## Requirements

- Python 3.12+
- ADB installed and in PATH
