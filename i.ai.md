# i.ai - AI Assistant for GRASS GIS

## DESCRIPTION

i.ai is an AI assistant that integrates Ollama LLM with GRASS GIS for comprehensive geospatial analysis and remote sensing workflows. It provides intelligent assistance with full knowledge of GRASS GIS modules, GDAL tools, and system utilities.

## KEY FEATURES

- **AI-Powered Analysis**: Uses Ollama LLM for intelligent geospatial guidance
- **Native GRASS Integration**: Works directly within GRASS GIS environment
- **Comprehensive Module Knowledge**: Knows all GRASS modules (i.*, g.*, r.*, v.*, db.*, t.*, m.*, r3.*, d.*, ps.*)
- **GDAL Tools Integration**: Access to GDAL command-line tools
- **System Tools**: Linux utilities and package management
- **Environment Awareness**: Detects current database, location, mapset, and available maps
- **Command Execution**: Can automatically execute suggested commands

## USAGE

### Basic Usage
```
i.ai "your question"
```

### With Options
```
i.ai "calculate NDVI" model=llama3.1:latest -e
```

### Interactive Mode
```
i.ai "help me with terrain analysis" -i
```

In interactive mode, type 'i.ai close' to return to GRASS CLI.

### System Information
```
i.ai "test" -s
```

## OPTIONS

### Parameters
- **query**: Your question or task for the AI assistant (required)
- **model**: Ollama model to use (default: llama3.1:latest)
- **ollama_url**: Ollama service URL (default: http://localhost:11434)
- **session**: Continue previous session (session ID)

### Flags
- **-e**: Execute suggested GRASS commands automatically
- **-v**: Verbose output
- **-i**: Interactive mode (stay in AI session)
- **-s**: Show system information

## EXAMPLES

### Remote Sensing
```
# Calculate NDVI
i.ai "calculate NDVI from Sentinel-2 bands"

# Land cover classification
i.ai "perform supervised classification on satellite image"

# Time series analysis
i.ai "create vegetation index time series from MODIS data"
```

### Terrain Analysis
```
# Basic terrain analysis
i.ai "calculate slope and aspect from DEM"

# Watershed delineation
i.ai "delineate watersheds from elevation data"

# Viewshed analysis
i.ai "perform viewshed analysis from observation points"
```

### Data Management
```
# Import data
i.ai "import GeoTIFF files into GRASS"

# Reprojection
i.ai "reproject data to UTM zone 33N"

# Export results
i.ai "export raster maps to GeoTIFF format"
```

### With Command Execution
```
# Execute suggested commands automatically
i.ai "import all GeoTIFF files and create NDVI" -e
```

## REQUIREMENTS

- GRASS GIS 8.0+
- Ollama service running with a model installed
- Python 3.8+
- Internet connection for AI queries

## SETUP

### Install Ollama
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull llama3.1:latest
```

### Install Addon
```bash
# Copy to GRASS addons directory
cp i.ai.py ~/.grass8/addons/scripts/i.ai
chmod +x ~/.grass8/addons/scripts/i.ai
```

## SYSTEM CAPABILITIES

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

### GDAL Tools
- Data processing: gdal_translate, gdalwarp, gdalbuildvrt
- Analysis: gdalinfo, gdaldem, gdal_calc.py
- Conversion: gdal_rasterize, gdal_polygonize

### System Tools
- Download: wget, curl
- Package management: apt, apt-cache
- Text processing: awk, sed, grep
- Archive: unzip, tar, gzip

## TROUBLESHOOTING

### Ollama Service Issues
```bash
# Check if running
ps aux | grep ollama

# Start service
ollama serve &

# Check models
ollama list
```

### Common Errors
- **"This module must be run from within GRASS GIS"**: Start GRASS with `grass --text`
- **"Ollama service is not running"**: Start Ollama service with `ollama serve &`
- **"Model not found"**: Download model with `ollama pull llama3.1:latest`

## AUTHOR

i.ai - AI Assistant for GRASS GIS

## SEE ALSO

- GRASS GIS reference manual
- Ollama documentation
- GDAL documentation
