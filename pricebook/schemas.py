"""
Field definitions, LOVs and status codes for the XXICS pricebook tables.

The Price Adjustments dictionaries below are extracted verbatim from the
workbook's _VBAFE_Services / _VBCS_Metadata sheets — they are the source of
truth for what the table accepts. The ``TABLES`` registry at the bottom is the
single place each page looks up its config.

NOTE: only Price Adjustments is fully defined. The other three tables
(_CHARGES, _DISCTERMS, _DISCADJS) still need their field / filter dicts pulled
from the same metadata sheets before their pages can run — see ``TABLES``.
"""

# Price Adjustments — fields, types, required flags
PRICEADJS_FIELDS = {
    "id":                 {"type": "string",  "pk": True,  "readonly": True},
    "batchno":            {"type": "string"},
    "pricelistname":      {"type": "string",  "required": True, "lov": "PRICELIST"},
    "itemname":           {"type": "string",  "required": True},
    "pricinguom":         {"type": "string",  "required": True, "lov": "UOM"},
    "baseprice":          {"type": "number",  "required": True},
    "chargestartdate":    {"type": "datetime"},
    "chargeenddate":      {"type": "datetime"},
    "customername":       {"type": "string"},
    "customernumber":     {"type": "string"},
    "shiptositename":     {"type": "string"},
    "customersitenumber": {"type": "string"},
    "adjustmenttype":     {"type": "string",  "lov": "ADJ_TYPE"},
    "adjustmentamount":   {"type": "number"},
    "adjustmentbasis":    {"type": "string"},
    "precedence":         {"type": "number"},
    "market":             {"type": "string"},
    "marketindex":        {"type": "string"},
    "age":                {"type": "string",  "lov": "AGE"},
    "spec":               {"type": "string"},
    "grade":              {"type": "string",  "lov": "GRADE"},
    "adjustmentstartdate":{"type": "datetime"},
    "adjustmentenddate":  {"type": "datetime"},
    "status":             {"type": "string",  "lov": "STATUS"},
    "status_msg":         {"type": "string",  "readonly": True},
    "excludefromcpprice": {"type": "number"},
    "external_system_ref_id": {"type": "string"},
}

LOVS = {
    "PRICELIST": ["CP_Market Price", "Corporate Price List",
                  "Transfer Price List", "Market Price"],
    "UOM":       ["CA", "EA", "ST", "BC", "BG", "LB", "PL", "TE", "FLB"],
    "ADJ_TYPE":  ["MARKUP_AMOUNT", "MARKUP_PERCENT", "PRICE_OVERRIDE"],
    "STATUS":    ["N", "U", "E", "S", "IN", "IU"],
    "AGE":       ["MILD", "MEDIUM", "SHARP"],
    "GRADE":     ["0", "100", "101", "102", "103", "104", "105"],
    "YESNO":     ["Y", "N"],
}

# Status code meanings (display in UI)
STATUS_LABELS = {
    "N":  "New",
    "U":  "Update Pending",
    "E":  "Error",
    "S":  "Succeeded",
    "IN": "Interface New",
    "IU": "Interface Update",
}

# User-facing READ filters (the columns marked "P" in the spec screenshot).
# Comparators:
#   Contains       -> case-insensitive substring match (single value)
#   ContainsAny    -> comma-separated values, OR-matched (substring each)
#   Equals         -> exact match (rendered as a dropdown when options provided)
#   In             -> multi-select; matches any of the chosen values
#   DateOnOrAfter  -> col >= picked date   (window start; Pacific->UTC)
#   DateOnOrBefore -> col <= picked date   (window end;   Pacific->UTC)
PRICEADJS_FILTERS = [
    ("itemname",            "ContainsAny"),      # comma = multiple item names
    ("customername",        "ContainsAny"),      # comma = multiple customer names
    ("shiptositename",      "ContainsAny"),      # comma = multiple ship-to sites
    ("customersitenumber",  "ContainsAny"),      # comma = multiple site numbers
    ("batchno",             "Contains"),         # find an upload batch
    ("market",              "Equals"),           # dropdown of distinct markets
    ("status",              "In"),               # multi-select dropdown (LOV STATUS)
    ("adjustmentstartdate", "DateOnOrAfter"),
    ("adjustmentenddate",   "DateOnOrBefore"),
]

# Fixed constraints always applied to the READ (the columns marked "D").
# These match the "market price" slice shown in the spec screenshot, so the
# user never has to set them. Shown in output but not offered as filters.
PRICEADJS_DEFAULTS = {
    "pricelistname":  "CP_Market Price",
    "pricinguom":     "EA",
    "adjustmenttype": "MARKUP_AMOUNT",
}


# --- Table registry ------------------------------------------------------
# The editor config keyed by table. ``fields`` and ``filters`` drive the generic
# editor (editor.py); ``resource`` is the ORDS path segment.
#
# Today the UI is fixed to Price Adjustments. The ``None`` stubs are placeholders
# for the other XXICS pricebook tables: to enable one, fill in its *_FIELDS and
# *_FILTERS dicts (same shape as Price Adjustments above), add a matching read_*
# function in ords_client.py, and point the view at it.
TABLES = {
    "priceadjs": {
        "title":    "Price Adjustments",
        "table":    "XXICS_PRICEBOOK_ADJUSTMENTS",
        "resource": "priceadjs",      # ORDS: {base_url}/priceadjs/{id}
        "fields":   PRICEADJS_FIELDS,
        "filters":  PRICEADJS_FILTERS,
        "defaults": PRICEADJS_DEFAULTS,
    },
    "pricecharges": None,   # XXICS_PRICEBOOK_CHARGES   — schema TBD
    "discterms":    None,   # XXICS_PRICEBOOK_DISCTERMS — schema TBD
    "discadjs":     None,   # XXICS_PRICEBOOK_DISCADJS  — schema TBD
}
