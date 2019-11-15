from collections import OrderedDict

# streams: API URL endpoints to be called
# properties:
#   <root node>: Plural stream name for the endpoint
#   path: API endpoint relative path, when added to the base URL, creates the full path,
#       default = stream_name
#   key_properties: Primary key fields for identifying an endpoint record.
#   replication_method: INCREMENTAL or FULL_TABLE
#   replication_keys: bookmark_field(s), typically a date-time, used for filtering the results
#        and setting the state
#   params: Query, sort, and other endpoint specific parameters; default = {}
#   data_key: JSON element containing the results list for the endpoint; default = root (no data_key)
#   bookmark_query_field: From date-time field used for filtering the query
#   bookmark_type: Data type for bookmark, integer or datetime

FILE_METADATA = {
    "api": "files",
    "path": "files/{spreadsheet_id}",
    "key_properties": ["id"],
    "replication_method": "INCREMENTAL",
    "replication_keys": ["modifiedTime"],
    "params": {
        "fields": "id,name,createdTime,modifiedTime,version,teamDriveId,driveId,lastModifyingUser"
    }
}

SPREADSHEET_METADATA = {
    "api": "sheets",
    "path": "spreadsheets/{spreadsheet_id}",
    "key_properties": ["spreadsheetId"],
    "replication_method": "FULL_TABLE",
    "params": {
        "includeGridData": "false"
    }
}

SHEET_METADATA = {
    "api": "sheets",
    "path": "spreadsheets/{spreadsheet_id}",
    "key_properties": ["sheetId"],
    "replication_method": "FULL_TABLE",
    "params": {
        "includeGridData": "true",
        "ranges": "'{sheet_title}'!1:2"
    }
}

SHEETS_LOADED = {
    "api": "sheets",
    "path": "spreadsheets/{spreadsheet_id}/values/'{sheet_title}'!{range_rows}",
    "data_key": "values",
    "key_properties": ["spreadsheetId", "sheetId", "loadDate"],
    "replication_method": "FULL_TABLE",
    "params": {
        "dateTimeRenderOption": "SERIAL_NUMBER",
        "valueRenderOption": "UNFORMATTED_VALUE",
        "majorDimension": "ROWS"
    }
}

# Ensure streams are ordered logically
STREAMS = OrderedDict()
STREAMS['file_metadata'] = FILE_METADATA
STREAMS['spreadsheet_metadata'] = SPREADSHEET_METADATA
STREAMS['sheet_metadata'] = SHEET_METADATA
STREAMS['sheets_loaded'] = SHEETS_LOADED