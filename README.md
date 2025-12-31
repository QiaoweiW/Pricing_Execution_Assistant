# VBCS Generation App

A Streamlit application for generating HTST & ESL Private Label VBCS files for Oracle upload.

## 🚀 Quick Start

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python -m streamlit run streamlit_app.py --server.headless true --server.port 8501
```

### Streamlit Cloud
The app is deployed at: [https://pricingexecutionagent-qiaoweiwang.streamlit.app](https://pricingexecutionagent-qiaoweiwang.streamlit.app)

## 🛠️ Features

### Four Main Tools
1. **Fixed Pricing** - Generate VBCS files for 'Fixed' and 'Quarterly' pricing items
2. **KS Pricing** - Generate VBCS files for KS pricing data
3. **Variable Pricing** - Generate VBCS files for variable pricing data
4. **Combine VBCS** - Combine multiple VBCS files into a single output

### Key Capabilities
- Upload CSV files through web interface
- Process data using existing Python scripts
- Download generated VBCS files
- Real-time processing status and error reporting
- Comprehensive debugging information

## 📁 Project Structure

```
Pricing_Execution_Agent/
├── streamlit_app.py              # Main Streamlit application
├── requirements.txt              # Python dependencies
├── README.md                     # This file
├── MAINTENANCE.md               # Detailed maintenance guide
├── data/                        # Output CSV files
└── HTST & ESL PL/VBCS_Generation/Archive/Command/Program/
    ├── Fixed_Pricing_VBCS.py     # Fixed pricing processing
    ├── KS_Pricing_VBCS.py        # KS pricing processing
    ├── Variable_Pricing_VBCS.py  # Variable pricing processing
    └── Combine_VBCS.py           # VBCS combination
```

## 📋 Usage

1. **Select Tool**: Click on one of the four pricing tools
2. **Upload Files**: Upload required CSV files for the selected tool
3. **Run Processing**: Click "Run [Tool] Generation" to process the data
4. **Download Results**: Download the generated VBCS CSV file

## 🔧 Maintenance

For detailed maintenance instructions, see [MAINTENANCE.md](MAINTENANCE.md).

## ⚠️ Limitations

- Custom models such as Bulk Milk (totes & tankers) and KS Organic milk are not covered
- Requires specific CSV file formats and naming conventions
- Processing scripts must be present in the designated directory

## 📞 Support

For technical issues or questions, refer to the maintenance guide or check the Streamlit Cloud deployment logs.