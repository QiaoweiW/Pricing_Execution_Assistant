# 📚 Pricing Execution Agent - Project Structure Guide

## 🎯 Overview
This is a **Streamlit web application** that helps the Darigold pricing team generate VBCS (Value-Based Cost Structure) files for Oracle upload. It's a modular, maintainable system that processes pricing data through multiple tools.

---

## 🏗️ Architecture Overview

```
Pricing_Execution_Agent/
│
├── 🎨 PRESENTATION LAYER (What users see)
│   ├── streamlit_app.py              # Main router - entry point
│   └── pages/                         # UI views for each page
│       ├── home_view.py
│       ├── new_price_quote_view.py
│       ├── market_barometer_view.py
│       ├── pricing_granularity_view.py
│       └── pricing_execution_automation_view.py
│
├── 🛠️ UTILITY LAYER (Shared helpers)
│   └── utils/
│       ├── ui_helpers.py             # CSS, styling, UI components
│       ├── data_helpers.py           # Data loading, preview functions
│       └── processing_helpers.py     # Script execution, file handling
│
├── ⚙️ PROCESSING LAYER (Business logic)
│   └── processing/
│       ├── Fixed_Pricing_VBCS.py
│       ├── KS_Pricing_VBCS.py
│       ├── Variable_Pricing_VBCS.py
│       ├── Combine_VBCS.py
│       └── new_pricing_processor.py
│
├── 🔌 EXTERNAL INTEGRATIONS
│   ├── fred_api_client.py            # FRED API integration
│   └── fred_api_config.py           # FRED API configuration
│
├── 📊 DATA & OUTPUT
│   ├── data/                         # Generated CSV files
│   └── example_files/                # Sample data files
│
└── 📖 DOCUMENTATION
    ├── README.md
    ├── MAINTENANCE.md
    ├── FRED_API_SETUP.md
    └── DEPLOYMENT_CHECKLIST.md
```

---

## 📁 File-by-File Breakdown

### 🎯 **Entry Point**

#### `streamlit_app.py` (91 lines)
**Role**: Application router and navigation controller

**What it does**:
- Sets up Streamlit page configuration
- Creates sidebar navigation menu
- Routes user to correct page based on selection
- Applies global CSS styling
- Renders footer

**Key Pattern**: **Router Pattern** - It doesn't contain business logic, just navigation

**Dependencies**: All page views, `utils.ui_helpers`

---

### 🎨 **Presentation Layer - Pages**

#### `pages/home_view.py` (52 lines)
**Role**: Landing page / dashboard

**What it does**:
- Welcomes users
- Provides quick links to other pages
- Shows overview of available tools

**UI Elements**: Headers, info boxes, quick start guide

---

#### `pages/new_price_quote_view.py` (345 lines)
**Role**: Price quote generation tool

**What it does**:
- Loads pricing database (parquet file)
- Provides query interface with filters (Plant, Volume, Pallet, etc.)
- Allows CSV file upload to refresh database
- Runs `new_pricing_processor.py` to process uploaded files
- Displays filtered results
- Provides download functionality

**Key Features**:
- Database connection status
- Multi-filter query system
- File upload and processing
- Data preview and download

**Dependencies**: `processing/new_pricing_processor.py`, pandas, tempfile

---

#### `pages/market_barometer_view.py` (47 lines)
**Role**: Market trend analysis (placeholder)

**What it does**:
- Currently shows placeholder message
- Designed for FRED API integration
- Will show market indicators and trends

**Future**: Will integrate with `fred_api_client.py`

---

#### `pages/pricing_granularity_view.py` (24 lines)
**Role**: Pricing granularity analysis (placeholder)

**What it does**:
- Currently shows placeholder message
- Designed for detailed pricing analysis

**Status**: Under development

---

#### `pages/pricing_execution_automation_view.py` (600+ lines)
**Role**: VBCS file generation hub - **MOST COMPLEX PAGE**

**What it does**:
- Provides 5 different tools:
  1. **Fixed Pricing** - Generates fixed pricing VBCS files
  2. **KS Pricing** - Generates Kirkland Signature pricing VBCS files
  3. **Variable Pricing** - Generates variable pricing VBCS files
  4. **Combine VBCS** - Combines multiple VBCS files
  5. **Pricing Update Validation** - Validates pricing changes (placeholder)

**Key Functions**:
- `run_fixed_pricing()` - Handles fixed pricing workflow
- `run_ks_pricing()` - Handles KS pricing workflow
- `run_variable_pricing()` - Handles variable pricing workflow
- `run_combine_vbcs()` - Combines VBCS files
- `run_pricing_validation()` - Placeholder for validation

**Dependencies**: 
- `utils.processing_helpers.run_processing_script()`
- `utils.data_helpers.load_existing_data()`
- Processing scripts in `processing/` folder

---

### 🛠️ **Utility Layer**

#### `utils/ui_helpers.py` (174 lines)
**Role**: Shared UI components and styling

**What it does**:
- `apply_custom_css()` - Applies global CSS styles
- `create_consistent_container()` - Creates styled containers
- `create_metric_box()` - Creates metric display boxes
- `render_footer()` - Renders app footer

**Used by**: All page views

---

#### `utils/data_helpers.py` (110 lines)
**Role**: Data loading and preview utilities

**What it does**:
- `load_existing_data()` - Loads VBCS CSV files from `data/` directory
- `display_data_summary()` - Shows summary of loaded files
- `display_data_preview()` - Shows preview of selected file

**Key Feature**: Searches multiple possible output directory locations

**Used by**: `pricing_execution_automation_view.py`

---

#### `utils/processing_helpers.py` (298 lines)
**Role**: Script execution and file processing - **CRITICAL FILE**

**What it does**:
- `cleanup_output_files()` - Cleans up old output files
- `run_processing_script()` - **MOST IMPORTANT FUNCTION**
  - Creates temporary directory
  - Saves uploaded files with encoding detection
  - Modifies processing scripts to use correct paths
  - Executes scripts via subprocess
  - Handles encoding issues (UTF-8, latin-1, etc.)
  - Copies output files to `data/` directory
  - Returns success/error messages

**Key Features**:
- Handles multiple file encodings (UTF-8, latin-1, cp1252)
- Modifies script paths dynamically
- Comprehensive error handling
- Debug information logging

**Used by**: `pricing_execution_automation_view.py`

**⚠️ RISK AREA**: This is where encoding errors occur!

---

### ⚙️ **Processing Layer**

#### `processing/Fixed_Pricing_VBCS.py`
**Role**: Generates VBCS files for fixed pricing items

**What it does**:
- Filters items with 'Fixed' or 'Quarterly' market index
- Excludes items starting with 'DG'
- Applies effective dates
- Generates VBCS format output

**Input Files**: `Old_Price_Build.csv`, `Effective_Date_Assumptions.csv`
**Output**: `fixed_vbcs.csv`

---

#### `processing/KS_Pricing_VBCS.py`
**Role**: Generates VBCS files for Kirkland Signature items

**What it does**:
- Filters KS items from Price Build Report
- Matches with Costco region-specific pricing
- Applies CLASS market index filtering
- Generates VBCS for EA and CA UOMs

**Input Files**: `Costco_HTST_Pricing.csv`, `Old_Price_Build.csv`, `Costco_HTST_Region_Lookup.csv`, `Effective_Date_Assumptions.csv`
**Output**: `ks_htst_vbcs.csv`

---

#### `processing/Variable_Pricing_VBCS.py`
**Role**: Generates VBCS files for variable pricing items

**What it does**:
- Processes execution data with UOM calculations
- Applies effective dates and market classifications
- Handles cross-dock logic for Winco and URM customers
- Generates separate files for different customer groups

**Input Files**: `Execution_final.csv`, `HTST Pricing_UOMS_v1.csv`, `Milk_Market_Index.csv`, `Effective_Date_Assumptions.csv`, `Customer_Extract_Report.csv`
**Output**: `urm_vbcs.csv`, `winco_vbcs.csv`, `batch_vbcs.csv`

---

#### `processing/Combine_VBCS.py`
**Role**: Combines multiple VBCS files into one

**What it does**:
- Reads multiple VBCS CSV files
- Combines them into single file
- Removes duplicates
- Keeps first 21 columns

**Input**: Multiple VBCS CSV files
**Output**: `combined_all_vbcs.csv`

---

#### `processing/new_pricing_processor.py`
**Role**: Processes uploaded CSV files to create pricing database

**What it does**:
- Reads 9 required CSV files
- Processes and combines them
- Creates parquet file for fast querying
- Used by New Price Quote page

**Input**: 9 CSV files (Product_Class_Plant.csv, etc.)
**Output**: `pricing_data.parquet` (in temp directory)

---

### 🔌 **External Integrations**

#### `fred_api_client.py`
**Role**: FRED API client for economic data

**What it does**:
- Connects to Federal Reserve Economic Data API
- Retrieves economic indicators
- Calculates percentage changes
- Creates charts

**Dependencies**: `fred_api_config.py`, `requests` library

**Status**: Partially integrated (Market Barometer page)

---

#### `fred_api_config.py`
**Role**: FRED API configuration

**What it does**:
- Stores API key (needs to be configured)
- Defines default indicators
- Sets chart configuration

**⚠️ SECURITY RISK**: Contains placeholder API key

---

### 📊 **Data Storage**

#### `data/` directory
**Role**: Output file storage

**What it contains**:
- Generated VBCS CSV files
- Files are created by processing scripts
- Files are loaded by `data_helpers.py`

**Files**:
- `fixed_vbcs.csv`
- `ks_htst_vbcs.csv`
- `urm_vbcs.csv`
- `winco_vbcs.csv`
- `batch_vbcs.csv`
- `combined_all_vbcs.csv`

---

## ⚠️ **CRITICAL RISKS & ISSUES**

### 🔴 **HIGH RISK**

#### 1. **Path Dependencies in Processing Scripts**
**Location**: `processing/*.py` files

**Problem**: 
- Scripts use `get_relative_path()` with hardcoded paths like `../../../../`
- These paths assume specific folder structure
- If folder structure changes, scripts break

**Example**:
```python
EXECUTION_FOLDER = get_relative_path('../../../../')
PL_FOLDER = get_relative_path('../../../../../Monthly Refreshed Data_Common/')
```

**Impact**: Scripts will fail if run from different directory or if folder structure changes

**Mitigation**: `processing_helpers.py` modifies these paths dynamically, but this is fragile

---

#### 2. **Encoding Issues**
**Location**: `utils/processing_helpers.py`, line 296

**Problem**:
- CSV files may have different encodings (UTF-8, latin-1, cp1252)
- Windows default encoding is 'charmap' which can't handle Unicode
- Emojis in error messages cause encoding errors

**Impact**: Scripts fail with encoding errors, especially on Windows

**Status**: ✅ **FIXED** - Now handles encoding safely

---

#### 3. **Hardcoded Script Paths**
**Location**: `utils/processing_helpers.py`, line 108

**Problem**:
```python
script_path = Path("HTST & ESL PL/VBCS_Generation/Archive/Command/Program") / f"{script_name}.py"
```

**Impact**: If this folder doesn't exist or is renamed, all processing fails

**Mitigation**: Scripts are copied to `processing/` folder, but original path is still hardcoded

---

#### 4. **Temporary File Cleanup**
**Location**: `utils/processing_helpers.py`

**Problem**:
- Creates temporary directories for processing
- If script crashes, temp files may not be cleaned up
- Could fill up disk space over time

**Impact**: Disk space issues, potential data leakage

**Status**: Uses `tempfile.TemporaryDirectory()` which should auto-cleanup, but not guaranteed if process crashes

---

#### 5. **API Key Security**
**Location**: `fred_api_config.py`

**Problem**:
- API key is in plain text in config file
- If committed to Git, key is exposed
- No environment variable fallback in all cases

**Impact**: API key could be stolen, leading to unauthorized API usage

**Mitigation**: Should use Streamlit secrets or environment variables

---

### 🟡 **MEDIUM RISK**

#### 6. **Subprocess Execution**
**Location**: `utils/processing_helpers.py`, line 210

**Problem**:
- Executes external Python scripts via subprocess
- No input validation on script paths
- Scripts can execute arbitrary code
- 5-minute timeout may be too long/short

**Impact**: Security risk if malicious scripts are uploaded, or scripts hang

**Mitigation**: Scripts are from trusted source, but no validation

---

#### 7. **Session State Management**
**Location**: All page views

**Problem**:
- Heavy use of `st.session_state` for filters and data
- No cleanup mechanism
- State persists across page refreshes
- Could cause memory issues with large datasets

**Impact**: Memory leaks, stale data, confusing user experience

---

#### 8. **File Upload Size Limits**
**Location**: All pages with file uploads

**Problem**:
- No explicit file size limits
- Large files could cause memory issues
- Streamlit has default 200MB limit, but not enforced

**Impact**: Application could crash with large files

---

#### 9. **Error Handling Gaps**
**Location**: Multiple files

**Problem**:
- Some functions don't handle all edge cases
- Error messages may not be user-friendly
- Some errors are silently caught

**Impact**: Users see cryptic errors, difficult to debug

---

### 🟢 **LOW RISK**

#### 10. **Documentation Out of Date**
**Location**: `README.md`, `MAINTENANCE.md`

**Problem**:
- README shows old structure (mentions old folder paths)
- Doesn't reflect new modular structure

**Impact**: Confusion for new developers

---

#### 11. **Unused Code**
**Location**: `mover_analysis_functions.py`, `fred_api_client.py`

**Problem**:
- Some modules are imported but not fully used
- Market Barometer page is placeholder
- Dead code increases maintenance burden

**Impact**: Code bloat, confusion

---

#### 12. **No Unit Tests**
**Location**: Entire project

**Problem**:
- No test files
- No test coverage
- Changes could break functionality without detection

**Impact**: Bugs go undetected, refactoring is risky

---

## 🎓 **Key Learning Points**

### 1. **Separation of Concerns**
- ✅ **Good**: UI (pages/) is separated from business logic (processing/)
- ✅ **Good**: Shared utilities are in utils/
- ⚠️ **Issue**: Some business logic still in view files

### 2. **Dependency Management**
- ✅ **Good**: Clear import structure
- ⚠️ **Issue**: Hardcoded paths create tight coupling

### 3. **Error Handling**
- ✅ **Good**: Try-except blocks in critical areas
- ⚠️ **Issue**: Some errors are swallowed, not logged

### 4. **Configuration Management**
- ⚠️ **Issue**: API keys in code, paths hardcoded
- ✅ **Good**: Some use of environment variables

### 5. **Data Flow**
```
User Upload → processing_helpers.py → Temporary Directory → 
Processing Script → Output Files → data/ directory → 
data_helpers.py → Display to User
```

---

## 🛡️ **Recommendations for Risk Mitigation**

### Immediate Actions:
1. ✅ **DONE**: Fix encoding issues in error handling
2. **TODO**: Move API keys to environment variables
3. **TODO**: Add file size validation
4. **TODO**: Add logging for debugging

### Short-term:
1. **TODO**: Add unit tests for critical functions
2. **TODO**: Update documentation
3. **TODO**: Add input validation
4. **TODO**: Implement proper error logging

### Long-term:
1. **TODO**: Refactor hardcoded paths to configuration
2. **TODO**: Add monitoring and alerting
3. **TODO**: Implement proper session cleanup
4. **TODO**: Add data validation layer

---

## 📝 **Quick Reference: Where to Make Changes**

| What You Want to Change | File to Edit |
|------------------------|--------------|
| Home page UI | `pages/home_view.py` |
| New Price Quote UI | `pages/new_price_quote_view.py` |
| VBCS Generator UI | `pages/pricing_execution_automation_view.py` |
| Global styling | `utils/ui_helpers.py` |
| Data loading logic | `utils/data_helpers.py` |
| Script execution | `utils/processing_helpers.py` |
| Fixed pricing logic | `processing/Fixed_Pricing_VBCS.py` |
| Navigation/routing | `streamlit_app.py` |
| API configuration | `fred_api_config.py` |

---

## 🎯 **Summary**

**Strengths**:
- ✅ Well-organized modular structure
- ✅ Clear separation of UI and business logic
- ✅ Good use of utilities for shared code
- ✅ Comprehensive error handling in critical paths

**Weaknesses**:
- ⚠️ Hardcoded paths create fragility
- ⚠️ Security concerns with API keys
- ⚠️ No testing infrastructure
- ⚠️ Some error handling could be improved

**Overall**: The structure is **good and maintainable**, but needs **hardening** for production use, especially around security and error handling.























