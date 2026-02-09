# i.ai - AI Assistant for GRASS GIS

**i.ai** is a native GRASS GIS addon that integrates Ollama LLM with GRASS GIS for comprehensive geospatial analysis and remote sensing workflows.

## Features

- **AI-Powered Analysis**: Uses Ollama LLM for intelligent geospatial guidance
- **Native GRASS Integration**: Works directly within GRASS GIS text environment
- **Comprehensive Module Knowledge**: Knows all GRASS modules (i.*, g.*, r.*, v.*, db.*, t.*, m.*, r3.*, d.*, ps.*)
- **GDAL Tools Integration**: Access to 24+ GDAL command-line tools
- **System Tools**: Linux utilities and package management (apt-cache)
- **Environment Awareness**: Detects current database, location, mapset, and available maps
- **Command Execution**: Can automatically execute suggested commands

## Quick Start

### 1. Install Addon
```bash
make MODULE_TOPDIR=$HOME/dev/grass
```

### 2. Start GRASS GIS
```bash
grass --text
```

### 3. Use i.ai
```bash
# Basic usage
GRASS> i.ai "your question"

# With automatic command execution
GRASS> i.ai "calculate NDVI" -e

# Show system information
GRASS> i.ai "test" -s

# Interactive mode
GRASS> i.ai "help me" -i

# In interactive mode, type 'i.ai close' to return to GRASS CLI
```

## Requirements

- GRASS GIS 8.0+
- Ollama service with a model installed
- Python 3.8+

### Setup Ollama
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Start service
ollama serve &

# Download model
ollama pull llama3.1:latest
```

## Usage Examples

### Remote Sensing
```bash
GRASS> i.ai "calculate NDVI from Sentinel-2 bands"
GRASS> i.ai "perform land cover classification"
GRASS> i.ai "create vegetation index time series"
```

### Terrain Analysis
```bash
GRASS> i.ai "calculate slope and aspect from DEM"
GRASS> i.ai "delineate watersheds"
GRASS> i.ai "perform viewshed analysis"
```

### Data Management
```bash
GRASS> i.ai "import GeoTIFF files"
GRASS> i.ai "reproject data to UTM"
GRASS> i.ai "export results"
```

## Options

### Parameters
- `query`: Your question or task (required)
- `model`: Ollama model to use (default: llama3.1:latest)
- `ollama_url`: Ollama service URL (default: http://localhost:11434)
- `session`: Continue previous session

### Flags
- `-e`: Execute suggested GRASS commands automatically
- `-v`: Verbose output
- `-i`: Interactive mode (stay in AI session)
- `-s`: Show system information

## System Capabilities

i.ai has comprehensive knowledge of:

### GRASS GIS Modules (110+ total)
- **Imagery (i.*)**: 14 modules for satellite image processing
- **General (g.*)**: 12 modules for data management
- **Raster (r.*)**: 16 modules for raster analysis
- **Vector (v.*)**: 15 modules for vector operations
- **Database (db.*)**: 9 modules for database operations
- **Temporal (t.*)**: 12 modules for time series analysis
- **3D Raster (r3.*)**: 9 modules for volumetric data
- **Display (d.*)**: 10 modules for visualization
- **PostScript (ps.*)**: 5 modules for map output

### GDAL Tools (24 available)
- Data processing: gdal_translate, gdalwarp, gdalbuildvrt
- Analysis: gdalinfo, gdaldem, gdal_calc.py
- Conversion: gdal_rasterize, gdal_polygonize

### System Tools
- Download: wget, curl
- Package management: apt, apt-cache
- Text processing: awk, sed, grep
- Archive: unzip, tar, gzip

## Demo

To see all system capabilities:
```bash
grass --text --exec python3 i.ai.py -s
```

## File Structure

```
i.ai/
├── i.ai.py               # Main GRASS addon
├── Makefile             # GRASS build system
├── i.ai.md              # Manual page
├── i.ai.html            # HTML documentation
└── README.md             # This file
```

## Troubleshooting

### Ollama Service Issues
```bash
# Check if running
ps aux | grep ollama

# Start service
ollama serve &

# Check models
ollama list
```

### GRASS Environment
```bash
# Must run from within GRASS
grass --text

# Check environment
g.gisenv
```

### Module Not Found
```bash
# Check installation
ls ~/.grass8*/addons/scripts/

# Manual installation
cp i.ai.py ~/.grass8*/addons/scripts/i.ai
chmod +x ~/.grass8*/addons/scripts/i.ai
```
