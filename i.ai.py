#!/usr/bin/env python3
"""
i.ai GRASS Addon - AI Assistant for GRASS GIS
Native GRASS addon that integrates with GRASS CLI interface
"""

#%module
#% description: AI assistant for GRASS GIS with comprehensive module knowledge
#% keyword: AI
#% keyword: assistant
#% keyword: analysis
#% keyword: remote sensing
#% keyword: automation
#%end

#%option
#% key: query
#% type: string
#% description: Your question or task for the AI assistant
#% required: yes
#%end

#%option
#% key: model
#% type: string
#% description: Ollama model to use
#% answer: llama3.1:latest
#%end

#%option
#% key: ollama_url
#% type: string
#% description: Ollama service URL
#% answer: http://localhost:11434
#%end

#%option
#% key: session
#% type: string
#% description: Continue previous session (session ID)
#%end

#%flag
#% key: e
#% description: Execute suggested GRASS commands automatically
#%end

#%flag
#% key: v
#% description: Verbose output
#%end

#%flag
#% key: i
#% description: Interactive mode (stay in AI session)
#%end

#%flag
#% key: s
#% description: Show system information
#%end

import sys
import os
import json
import subprocess
import requests
import re
import tempfile
import shutil
from pathlib import Path

# GRASS imports
import grass.script as gs
from grass.exceptions import CalledModuleError

class IAIGrassAddon:
    def __init__(self):
        self.session_context = {}
        self.module_cache = {}
        self.system_info = {}
        
    def get_grass_modules(self):
        """Get comprehensive list of GRASS modules"""
        if self.module_cache:
            return self.module_cache
            
        modules = {
            'imagery': [],      # i.*
            'general': [],      # g.*
            'raster': [],       # r.*
            'raster3d': [],     # r3.*
            'vector': [],       # v.*
            'database': [],     # db.*
            'temporal': [],     # t.*
            'misc': [],         # m.*
            'display': [],      # d.*
            'ps': [],           # ps.*
        }
        
        try:
            # Get all modules using g.version -g
            result = gs.read_command('g.version', flags='g')
            # Parse modules from version info or use hardcoded list
            all_modules = [
                'g.list', 'g.remove', 'g.copy', 'g.rename', 'g.region', 'g.proj', 'g.gisenv',
                'r.in.gdal', 'r.out.gdal', 'r.info', 'r.stats', 'r.univar', 'r.mapcalc',
                'r.slope.aspect', 'r.watershed', 'r.resample', 'r.rescale', 'r.colors',
                'v.in.ogr', 'v.out.ogr', 'v.info', 'v.db.select', 'v.db.addcolumn', 'v.buffer',
                'v.overlay', 'v.select', 'v.centroid', 'v.voronoi', 'v.clean',
                'i.group', 'i.target', 'i.class', 'i.cluster', 'i.maxlik', 'i.smap',
                'i.vi', 'i.tasscap', 'i.pca', 'i.fft', 'i.ifft',
                'db.connect', 'db.select', 'db.execute', 'db.tables', 'db.columns',
                'db.describe', 'db.drivers', 'db.login',
                't.create', 't.register', 't.info', 't.list', 't.remove', 't.sample',
                't.rast.aggregate', 't.rast.extract', 't.vect.observe', 't.vect.what.strds',
                'm.proj', 'm.cogo', 'm.transform', 'm.measure', 'm.lpp'
            ]
            
            # Categorize modules by prefix
            for module in all_modules:
                if module.startswith('i.'):
                    modules['imagery'].append(module)
                elif module.startswith('g.'):
                    modules['general'].append(module)
                elif module.startswith('r.'):
                    modules['raster'].append(module)
                elif module.startswith('r3.'):
                    modules['raster3d'].append(module)
                elif module.startswith('v.'):
                    modules['vector'].append(module)
                elif module.startswith('db.'):
                    modules['database'].append(module)
                elif module.startswith('t.'):
                    modules['temporal'].append(module)
                elif module.startswith('m.'):
                    modules['misc'].append(module)
                elif module.startswith('d.'):
                    modules['display'].append(module)
                elif module.startswith('ps.'):
                    modules['ps'].append(module)
                    
        except Exception as e:
            gs.warning(f"Could not enumerate modules: {e}")
            
        self.module_cache = modules
        return modules
    
    def get_gdal_tools(self):
        """Get available GDAL command-line tools"""
        gdal_tools = []
        common_tools = [
            'gdalinfo', 'gdal_translate', 'gdalwarp', 'gdalbuildvrt',
            'gdal_rasterize', 'gdal_polygonize', 'gdal_sieve', 'gdal_fillnodata',
            'gdal_contour', 'gdaldem', 'gdal_grid', 'gdal_merge',
            'gdal_pansharpen', 'gdaltransform', 'nearblack', 'gdaladdo',
            'gdal_edit', 'gdal_calc.py', 'gdal_proximity', 'gdal_slope',
            'gdal_aspect', 'gdal_hillshade', 'gdal_roughness', 'gdal_TPI',
            'gdal_tri', 'ogrinfo', 'ogr2ogr', 'ogrtindex', 'ogrmerge',
            'ogrlineref', 'ogrlayer2sql', 'ogrlayer2sqlite', 'ogr2vrt'
        ]
        
        for tool in common_tools:
            if shutil.which(tool):
                gdal_tools.append(tool)
                
        return gdal_tools
    
    def get_system_info(self):
        """Get comprehensive system information"""
        if self.system_info:
            return self.system_info
            
        info = {
            'os': 'Linux',
            'grass_version': None,
            'grass_db': None,
            'grass_location': None,
            'grass_mapset': None,
            'grass_region': None,
            'available_maps': {'raster': [], 'vector': []},
            'gdal_tools': [],
            'python_packages': [],
            'system_tools': [],
        }
        
        try:
            # GRASS info
            gisenv = gs.gisenv()
            info['grass_version'] = gs.version()['version']
            info['grass_db'] = gisenv.get('GISDBASE')
            info['grass_location'] = gisenv.get('LOCATION_NAME')
            info['grass_mapset'] = gisenv.get('MAPSET')
            
            # Region info
            try:
                region = gs.region()
                info['grass_region'] = {
                    'north': region.get('n'),
                    'south': region.get('s'),
                    'east': region.get('e'),
                    'west': region.get('w'),
                    'nsres': region.get('nsres'),
                    'ewres': region.get('ewres'),
                    'rows': region.get('rows'),
                    'cols': region.get('cols')
                }
            except:
                pass
            
            # Available maps
            try:
                info['available_maps']['raster'] = gs.list_strings(type='raster')[:20]
                info['available_maps']['vector'] = gs.list_strings(type='vector')[:20]
            except:
                pass
            
            # GDAL tools
            info['gdal_tools'] = self.get_gdal_tools()
            
            # Python packages
            python_packages = []
            for pkg in ['gdal', 'rasterio', 'geopandas', 'numpy', 'scipy', 'matplotlib', 'sklearn']:
                try:
                    __import__(pkg)
                    python_packages.append(pkg)
                except ImportError:
                    pass
            info['python_packages'] = python_packages
            
            # System tools
            system_tools = []
            for tool in ['wget', 'curl', 'unzip', 'tar', 'gzip', 'awk', 'sed', 'grep', 'cut', 'sort', 'uniq']:
                if shutil.which(tool):
                    system_tools.append(tool)
            info['system_tools'] = system_tools
            
        except Exception as e:
            gs.warning(f"Could not get system info: {e}")
            
        self.system_info = info
        return info
    
    def search_packages(self, query):
        """Search for packages using apt-cache"""
        try:
            result = subprocess.run(
                ['apt-cache', 'search', query],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                return result.stdout
            else:
                return f"Package search failed: {result.stderr}"
        except Exception as e:
            return f"Package search error: {e}"
    
    def get_system_prompt(self):
        """Get comprehensive system prompt for AI"""
        modules = self.get_grass_modules()
        system_info = self.get_system_info()
        
        prompt = f"""
You are i.ai, an expert AI assistant for GRASS GIS and Remote Sensing analysis.

ENVIRONMENT CONTEXT:
- Operating System: Linux (Debian-based)
- GRASS GIS Version: {system_info['grass_version']}
- Database: {system_info['grass_db']}
- Location: {system_info['grass_location']}
- Mapset: {system_info['grass_mapset']}

CURRENT REGION:
{json.dumps(system_info['grass_region'], indent=2) if system_info['grass_region'] else 'Region not available'}

AVAILABLE GRASS MODULES:
Imagery (i.*): {', '.join(modules['imagery'][:10])} ({len(modules['imagery'])} total)
General (g.*): {', '.join(modules['general'][:10])} ({len(modules['general'])} total)
Raster (r.*): {', '.join(modules['raster'][:15])} ({len(modules['raster'])} total)
Raster3D (r3.*): {', '.join(modules['raster3d'])} ({len(modules['raster3d'])} total)
Vector (v.*): {', '.join(modules['vector'][:15])} ({len(modules['vector'])} total)
Database (db.*): {', '.join(modules['database'])} ({len(modules['database'])} total)
Temporal (t.*): {', '.join(modules['temporal'])} ({len(modules['temporal'])} total)
Miscellaneous (m.*): {', '.join(modules['misc'])} ({len(modules['misc'])} total)

AVAILABLE MAPS:
Raster: {', '.join(system_info['available_maps']['raster'])}
Vector: {', '.join(system_info['available_maps']['vector'])}

GDAL TOOLS:
{', '.join(system_info['gdal_tools'])}

PYTHON PACKAGES:
{', '.join(system_info['python_packages'])}

SYSTEM TOOLS:
{', '.join(system_info['system_tools'])}

CAPABILITIES:
1. Full access to all GRASS GIS modules (i.*, g.*, r.*, r3.*, v.*, db.*, t.*, m.*, d.*, ps.*)
2. GDAL command-line tools for raster/vector processing
3. Linux terminal tools (wget, curl, unzip, tar, awk, sed, grep, etc.)
4. Python libraries for geospatial analysis
5. Package installation via apt-cache search and apt install
6. File system operations and data download

RESPONSE GUIDELINES:
1. Provide specific, executable GRASS commands with correct syntax
2. Include parameter values where appropriate
3. Suggest GDAL tools when relevant for format conversion or processing
4. Recommend Linux tools for data download and preparation
5. Use apt-cache search to find additional tools when needed
6. Consider the current region and available maps in recommendations
7. Provide step-by-step workflows for complex analyses
8. Include error checking and validation suggestions

COMMAND SYNTAX EXAMPLES:
- GRASS: g.list type=raster
- GDAL: gdalinfo input.tif
- Linux: wget https://example.com/data.zip
- Package: apt-cache search satellite

Be practical, specific, and focus on implementable solutions using the available tools.
"""
        return prompt
    
    def query_ollama(self, query, model, url, session_context=""):
        """Query Ollama with comprehensive context"""
        try:
            system_prompt = self.get_system_prompt()
            full_prompt = f"{system_prompt}\n\nPrevious context: {session_context}\n\nUser query: {query}"
            
            payload = {
                "model": model,
                "prompt": full_prompt,
                "stream": False
            }
            
            response = requests.post(f"{url}/api/generate", json=payload, timeout=120)
            
            if response.status_code == 200:
                result = response.json()
                return result.get("response", "No response received")
            else:
                return f"Error: Failed to get response from Ollama (HTTP {response.status_code})"
                
        except Exception as e:
            return f"Error: {str(e)}"
    
    def extract_and_execute_commands(self, response):
        """Extract and execute GRASS/GDAL/Linux commands"""
        # Pattern to match command lines
        command_patterns = [
            r'\b(g\.\w+(?:\s+[^\s\n]+)*)',  # GRASS commands
            r'\b(r\.\w+(?:\s+[^\s\n]+)*)',  # Raster commands
            r'\b(v\.\w+(?:\s+[^\s\n]+)*)',  # Vector commands
            r'\b(i\.\w+(?:\s+[^\s\n]+)*)',  # Imagery commands
            r'\b(db\.\w+(?:\s+[^\s\n]+)*)', # Database commands
            r'\b(t\.\w+(?:\s+[^\s\n]+)*)',  # Temporal commands
            r'\b(m\.\w+(?:\s+[^\s\n]+)*)',  # Misc commands
            r'\b(gdal\w+(?:\s+[^\s\n]+)*)',  # GDAL commands
            r'\b(wget|curl|unzip|tar|gzip|awk|sed|grep)(?:\s+[^\s\n]+)*',  # Linux tools
        ]
        
        executed_commands = []
        
        for pattern in command_patterns:
            matches = re.findall(pattern, response, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                if match not in executed_commands:
                    executed_commands.append(match)
        
        if executed_commands:
            gs.message(f"\n🔧 Executing {len(executed_commands)} commands:")
            
            for i, cmd in enumerate(executed_commands, 1):
                gs.message(f"\n{i}. {cmd}")
                
                try:
                    if cmd.startswith(('g.', 'r.', 'v.', 'i.', 'db.', 't.', 'm.', 'd.', 'ps.', 'r3.')):
                        # GRASS command
                        parts = cmd.split()
                        module = parts[0]
                        params = {}
                        
                        for part in parts[1:]:
                            if '=' in part:
                                key, value = part.split('=', 1)
                                params[key] = value
                            else:
                                params['input'] = part
                        
                        result = gs.read_command(module, **params)
                        gs.message("✅ Success:")
                        gs.message(result[:500] + "..." if len(result) > 500 else result)
                        
                    elif cmd.startswith('gdal') or any(tool in cmd for tool in ['wget', 'curl', 'unzip', 'tar', 'gzip', 'awk', 'sed', 'grep']):
                        # System command
                        result = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=60)
                        if result.returncode == 0:
                            gs.message("✅ Success:")
                            gs.message(result.stdout[:500] + "..." if len(result.stdout) > 500 else result.stdout)
                        else:
                            gs.warning(f"❌ Failed: {result.stderr}")
                    else:
                        gs.warning(f"⚠️ Unknown command type: {cmd}")
                        
                except Exception as e:
                    gs.warning(f"❌ Error executing {cmd}: {e}")
        
        return executed_commands

def main():
    query = options['query']
    model = options['model']
    ollama_url = options['ollama_url']
    session_id = options['session']
    execute_flag = flags['e']
    verbose = flags['v']
    interactive = flags['i']
    show_system = flags['s']
    
    iai = IAIGrassAddon()
    
    # Show system information if requested
    if show_system:
        system_info = iai.get_system_info()
        gs.message("🌍 i.ai System Information:")
        gs.message("=" * 50)
        gs.message(f"GRASS Version: {system_info['grass_version']}")
        gs.message(f"Database: {system_info['grass_db']}")
        gs.message(f"Location: {system_info['grass_location']}")
        gs.message(f"Mapset: {system_info['grass_mapset']}")
        gs.message(f"GDAL Tools: {len(system_info['gdal_tools'])} available")
        gs.message(f"Python Packages: {len(system_info['python_packages'])} available")
        gs.message(f"System Tools: {len(system_info['system_tools'])} available")
        
        modules = iai.get_grass_modules()
        total_modules = sum(len(modules[cat]) for cat in modules)
        gs.message(f"GRASS Modules: {total_modules} total")
        return
    
    # Check Ollama service
    try:
        response = requests.get(f"{ollama_url}/api/tags", timeout=5)
        if response.status_code != 200:
            gs.fatal("❌ Ollama service is not running. Start it with: ollama serve &")
    except:
        gs.fatal("❌ Cannot connect to Ollama service")
    
    # Get session context
    session_context = ""
    if session_id:
        # In a real implementation, you'd load session context from storage
        session_context = f"Continuing session {session_id}"
    
    if verbose:
        gs.message("🌍 i.ai - AI Assistant for GRASS GIS")
        gs.message(f"🤖 Using model: {model}")
        gs.message("📊 Analyzing query...")
    
    # Query AI
    ai_response = iai.query_ollama(query, model, ollama_url, session_context)
    
    # Display response
    gs.message("\n" + "="*70)
    gs.message("🤖 AI Response:")
    gs.message("="*70)
    gs.message(ai_response)
    gs.message("="*70)
    
    # Execute commands if requested
    if execute_flag:
        commands = iai.extract_and_execute_commands(ai_response)
        if not commands:
            gs.message("ℹ️ No executable commands found in response")
    
    # Interactive mode
    if interactive:
        gs.message("\n🔄 Interactive mode - Type 'i.ai close' to return to GRASS CLI")
        while True:
            try:
                user_input = input("\ni.ai> ").strip()
                if user_input.lower() in ['i.ai close', 'close', 'quit', 'exit', 'q']:
                    gs.message("\n👋 Returning to GRASS GIS CLI...")
                    break
                
                if user_input:
                    response = iai.query_ollama(user_input, model, ollama_url, session_context)
                    gs.message("\n" + "="*50)
                    gs.message("🤖 AI Response:")
                    gs.message("="*50)
                    gs.message(response)
                    gs.message("="*50)
                    
                    if execute_flag:
                        iai.extract_and_execute_commands(response)
                        
            except KeyboardInterrupt:
                break
        
        gs.message("👋 Exiting interactive mode")

if __name__ == "__main__":
    try:
        from grass.script import parser as grass_parser
        options, flags = grass_parser()
        main()
    except ImportError:
        print("This module must be run from within GRASS GIS")
        print("Start GRASS GIS with: grass --text")
        print("Then run: python3 iai_grass_addon.py query='your question'")
        sys.exit(1)
