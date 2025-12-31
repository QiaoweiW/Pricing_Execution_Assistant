# VBCS Generation App - Maintenance Guide

## 📋 Overview
This document provides comprehensive maintenance instructions for the VBCS Generation Streamlit application.

## 🏗️ Project Structure
```
Pricing_Execution_Agent/
├── streamlit_app.py              # Main Streamlit application
├── requirements.txt              # Python dependencies
├── README.md                     # Project overview
├── MAINTENANCE.md               # This maintenance guide
├── data/                        # Output CSV files directory
│   ├── fixed_vbcs.csv
│   ├── ks_htst_vbcs.csv
│   ├── urm_vbcs.csv
│   ├── winco_vbcs.csv
│   ├── batch_vbcs.csv
│   └── combined_all_vbcs.csv
└── HTST & ESL PL/VBCS_Generation/Archive/Command/Program/
    ├── Fixed_Pricing_VBCS.py     # Fixed pricing processing script
    ├── KS_Pricing_VBCS.py        # KS pricing processing script
    ├── Variable_Pricing_VBCS.py  # Variable pricing processing script
    ├── Combine_VBCS.py           # VBCS combination script
    ├── requirements.txt          # Script dependencies
    └── README.md                 # Script documentation
```

## 🚀 Deployment

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python -m streamlit run streamlit_app.py --server.headless true --server.port 8501
```

### Streamlit Cloud Deployment
1. **Repository**: `https://github.com/Biubiuwang123/Pricing_Execution_Agent`
2. **Main file**: `streamlit_app.py`
3. **Branch**: `main`
4. **Requirements**: `requirements.txt`

## 🔧 Maintenance Tasks

### 1. Regular Updates

#### Weekly Tasks
- [ ] Check Streamlit Cloud deployment status
- [ ] Verify data directory has latest output files
- [ ] Test all four pricing tools with sample data
- [ ] Review error logs in Streamlit Cloud
- [ ] Verify automatic cleanup is working (output files should be cleared before each run)

#### Monthly Tasks
- [ ] Update dependencies in `requirements.txt`
- [ ] Test with new data files
- [ ] Review and update documentation
- [ ] Check for Streamlit version updates

### 2. Data Management

#### Input Data Requirements
- **Fixed Pricing**: `Old_Price_Build.csv`, `Effective_Date_Assumptions.csv`
- **KS Pricing**: `Costco_Price_File.csv`, `Effective_Date_Assumptions.csv`
- **Variable Pricing**: `Variable_Pricing_File.csv`, `Effective_Date_Assumptions.csv`
- **Combine VBCS**: Any combination of the above output files

#### Output Data
- All generated files are saved to the `data/` directory
- Files are automatically copied from temporary processing directories
- Output files follow naming convention: `{type}_vbcs.csv`
- **Automatic Cleanup**: Output files are automatically cleaned up before each processing run
- **Manual Cleanup**: Use the "Clear All Output Files" button in the Combine VBCS tool for manual cleanup

### 3. Troubleshooting

#### Common Issues

**1. Script Execution Failures**
- **Symptom**: "Script failed" error with debug information
- **Cause**: File path issues or permission problems
- **Solution**: Check debug output for specific error details

**2. File Not Found Errors**
- **Symptom**: "No data found" messages
- **Cause**: Missing input files or incorrect file names
- **Solution**: Ensure uploaded files match expected names exactly

**3. Permission Denied Errors**
- **Symptom**: "[Errno 13] Permission denied"
- **Cause**: Script trying to write to restricted directory
- **Solution**: Script now writes to temp directory (fixed in latest version)

**4. Arrow Serialization Errors**
- **Symptom**: PyArrow conversion errors in terminal
- **Cause**: Data type compatibility issues
- **Solution**: These are warnings and don't affect functionality

#### Debug Information
The app provides detailed debug information when scripts fail:
- Script name and working directory
- Return code and error messages
- Files present in temporary directory
- Standard output and error streams

### 4. Code Maintenance

#### Key Files to Monitor
- `streamlit_app.py`: Main application logic
- `run_processing_script()`: Script execution function
- `load_existing_data()`: Data loading function
- Individual pricing functions: `run_fixed_pricing()`, etc.

#### Script Modifications
When updating processing scripts:
1. Test locally first
2. Update path modifications in `run_processing_script()`
3. Verify output file naming conventions
4. Test all four tools before deploying

### 5. Security Considerations

#### File Upload Security
- Only CSV files are accepted
- Files are processed in temporary directories
- No persistent storage of uploaded files
- Scripts run in isolated temporary environments

#### Access Control
- Streamlit Cloud deployment is public
- No authentication required
- Consider adding authentication for production use

### 6. Performance Optimization

#### Current Performance
- Scripts run with 5-minute timeout
- Temporary directories are cleaned up automatically
- Output files are copied efficiently

#### Optimization Opportunities
- Add progress bars for long-running scripts
- Implement caching for frequently used data
- Add parallel processing for multiple scripts

### 7. Monitoring and Logging

#### Streamlit Cloud Logs
- Access logs through Streamlit Cloud dashboard
- Monitor for errors and performance issues
- Check deployment status regularly

#### Local Development
- Use `st.sidebar.write()` for debug information
- Monitor terminal output for warnings
- Check data directory for output files

## 🔄 Update Procedures

### Updating Dependencies
1. Test new versions locally
2. Update `requirements.txt`
3. Commit and push changes
4. Monitor Streamlit Cloud deployment

### Updating Processing Scripts
1. Modify scripts in `HTST & ESL PL/VBCS_Generation/Archive/Command/Program/`
2. Update path modifications in `streamlit_app.py` if needed
3. Test all functionality locally
4. Deploy to Streamlit Cloud

### Adding New Features
1. Develop and test locally
2. Update documentation
3. Test with sample data
4. Deploy and monitor

## 📞 Support

### For Technical Issues
1. Check this maintenance guide
2. Review Streamlit Cloud logs
3. Test locally to isolate issues
4. Check GitHub repository for latest updates

### For Data Issues
1. Verify input file formats
2. Check file naming conventions
3. Review script requirements
4. Test with known good data

## 📝 Change Log

### Version 1.0 (Current)
- Initial deployment with four pricing tools
- File upload and processing functionality
- Streamlit Cloud deployment
- Comprehensive error handling and debugging

### Future Enhancements
- User authentication
- Progress bars for long operations
- Data validation and error checking
- Export functionality for multiple formats
- Automated testing suite

---

**Last Updated**: September 2024  
**Maintained By**: Pricing Team  
**Repository**: https://github.com/Biubiuwang123/Pricing_Execution_Agent
