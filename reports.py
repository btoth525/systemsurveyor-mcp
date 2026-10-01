"""Config builder for System Surveyor's own (server-rendered) reports. The defaults are the web app's own report settings
with no company or personal data in them."""
import copy
import json

REPORT_TYPES = ["Layout", "Bill of Materials", "Elements", "Cables", "Photo Tour", "Element Detail"]
XLSX_TYPES = {"Bill of Materials", "Elements", "Cables"}
COUNT_BY = ["Element Name", "Installation Status", "Model", "Model + Installation Status", "Descriptive Label"]
STATUSES = ["In place", "Proposed", "To be upgraded", "To be replaced", "To be removed"]
KEY = {"Layout": "layoutReport", "Bill of Materials": "billOfMaterialsReport", "Elements": "elementsReport", "Cables": "cablesReport",
       "Photo Tour": "photoTourReport", "Element Detail": "elementDetailReport"}

DEFAULT_CONFIG = json.loads(r'''
{
 "groupBy": "",
 "clientName": "",
 "hasSummary": false,
 "outputType": "pdf",
 "cablesReport": {
  "order": "",
  "sortField": "",
  "selectedView": "Cable Summary",
  "surveyLocation": "",
  "selectedFilters": {},
  "surveyDescription": ""
 },
 "filterValues": [],
 "hasCoverPage": true,
 "hasWatermark": false,
 "layoutReport": {
  "countBy": "Element Name",
  "watermark": "",
  "clientName": "",
  "showCompany": true,
  "showReportBy": true,
  "showSiteName": true,
  "useProReport": false,
  "showWatermark": false,
  "showClientName": false,
  "selectedFilters": {},
  "showCompanyLogo": true
 },
 "watermarkText": "",
 "elementsReport": {
  "order": 1,
  "groupBy": [],
  "sortField": "Element Name",
  "surveyLocation": "",
  "selectedColumns": [
   {
    "id": "Element Name",
    "label": "Element Name",
    "minWidth": 200,
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Formatted ID",
    "label": "Element ID",
    "minWidth": 120,
    "defaultColumn": true
   },
   {
    "id": "Component Manufacturer",
    "label": "Manufacturer",
    "minWidth": 130,
    "defaultColumn": true
   },
   {
    "id": "Component Model #",
    "label": "Model",
    "minWidth": 80,
    "defaultColumn": true
   },
   {
    "id": "Descriptive Label",
    "label": "Descriptive Label",
    "minWidth": 160,
    "defaultColumn": true
   },
   {
    "id": "Element Quantity",
    "label": "Quantity",
    "minWidth": 100,
    "textAlign": "center",
    "defaultColumn": true
   },
   {
    "id": "System Type",
    "label": "System Type",
    "minWidth": 150,
    "defaultColumn": true,
    "mandatoryColumn": true
   }
  ],
  "selectedFilters": {},
  "showAccessories": false,
  "surveyDescription": ""
 },
 "photoTourReport": {
  "showModel": true,
  "showNotes": false,
  "itemsPerRow": 4,
  "showLocation": true,
  "showSurveyName": true,
  "selectedFilters": {},
  "showDescription": true,
  "showManufacturer": true
 },
 "showCompanyLogo": true,
 "showSiteAddress": true,
 "shouldRefreshToken": true,
 "elementDetailReport": {
  "sections": [
   {
    "id": "accessories",
    "isVisible": true
   },
   {
    "id": "photos",
    "isVisible": true
   },
   {
    "id": "notes",
    "isVisible": true
   },
   {
    "id": "installation",
    "isVisible": true
   },
   {
    "id": "functional",
    "isVisible": true
   },
   {
    "id": "activity",
    "isVisible": true
   },
   {
    "id": "maintenance",
    "isVisible": true
   },
   {
    "id": "activityLog",
    "isVisible": true
   }
  ],
  "hideBlankFields": false,
  "selectedFilters": {}
 },
 "billOfMaterialsReport": {
  "order": 1,
  "showTotal": true,
  "sortField": "Element Name",
  "surveyLocation": "",
  "selectedColumns": [
   {
    "id": "Element Name",
    "label": "Element Name",
    "minWidth": 190,
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Component Model #",
    "label": "Model",
    "minWidth": 80,
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Component Manufacturer",
    "label": "Manufacturer",
    "minWidth": 130,
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Element Quantity",
    "label": "Quantity",
    "minWidth": 100,
    "textAlign": "center",
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Device Price",
    "info": "Unit Price = Average Device Price.\nPrice of $0 is excluded.",
    "label": "Unit Price",
    "minWidth": 100,
    "textAlign": "center",
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Installation Hours",
    "info": "The Labor Hours are multiplied by the Labor Rate to calculate the Extended Price.\nLabor Rate can be set in the Account Settings. The default value is $1.",
    "label": "Labor Hours",
    "minWidth": 120,
    "textAlign": "center",
    "defaultColumn": true,
    "mandatoryColumn": true
   },
   {
    "id": "Ext Price",
    "info": "Extended Price is a total calculated cost.\nQuantity x Unit Price + Labor Hours x Labor Rate = Extended Price.",
    "label": "Ext Price",
    "minWidth": 120,
    "textAlign": "center",
    "defaultColumn": true
   }
  ],
  "selectedFilters": {},
  "surveyDescription": ""
 }
}''')


def build_config(types, output="pdf", client_name="", cover_page=True, watermark="", legend=False, count_by="Element Name",
                 system_types=None, statuses=None, show_company_logo=True):
    """The config object System Surveyor's report service expects. legend=True turns on the layout report's own
    'Title Block & Legend' (the key of devices and counts)."""
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["outputType"] = output
    c["clientName"] = client_name
    c["hasCoverPage"] = bool(cover_page)
    c["hasWatermark"] = bool(watermark)
    c["watermarkText"] = watermark
    c["showCompanyLogo"] = bool(show_company_logo)
    lay = c["layoutReport"]
    lay["useProReport"] = bool(legend)
    lay["countBy"] = count_by
    lay["showCompanyLogo"] = bool(show_company_logo)
    lay["clientName"] = client_name
    lay["showClientName"] = bool(client_name)
    lay["watermark"] = watermark
    lay["showWatermark"] = bool(watermark)
    flt = {}
    if system_types:
        flt["System Type"] = list(system_types)
    if statuses:
        flt["Installation Status"] = list(statuses)
    if flt:
        for t in types:
            if t in ("Layout", "Elements", "Photo Tour", "Cables"):
                c[KEY[t]]["selectedFilters"] = copy.deepcopy(flt)
            elif t == "Bill of Materials":
                c[KEY[t]]["selectedFilters"] = {"Model": [], **copy.deepcopy(flt)}
    return c
