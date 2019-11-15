import time
import math
import singer
import json
import pytz
from datetime import datetime, timedelta
from collections import OrderedDict
from singer import metrics, metadata, Transformer, utils
from singer.utils import strptime_to_utc, strftime
from tap_google_sheets.streams import STREAMS
from tap_google_sheets.schema import get_sheet_metadata

LOGGER = singer.get_logger()


def write_schema(catalog, stream_name):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    try:
        singer.write_schema(stream_name, schema, stream.key_properties)
    except OSError as err:
        LOGGER.info('OS Error writing schema for: {}'.format(stream_name))
        raise err


def write_record(stream_name, record, time_extracted):
    try:
        singer.messages.write_record(stream_name, record, time_extracted=time_extracted)
    except OSError as err:
        LOGGER.info('OS Error writing record for: {}'.format(stream_name))
        LOGGER.info('record: {}'.format(record))
        raise err


def get_bookmark(state, stream, default):
    if (state is None) or ('bookmarks' not in state):
        return default
    return (
        state
        .get('bookmarks', {})
        .get(stream, default)
    )


def write_bookmark(state, stream, value):
    if 'bookmarks' not in state:
        state['bookmarks'] = {}
    state['bookmarks'][stream] = value
    LOGGER.info('Write state for stream: {}, value: {}'.format(stream, value))
    singer.write_state(state)


# Transform/validate batch of records w/ schema and sent to target
def process_records(catalog,
                    stream_name,
                    records,
                    time_extracted):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    stream_metadata = metadata.to_map(stream.metadata)
    with metrics.record_counter(stream_name) as counter:
        for record in records:
            # Transform record for Singer.io
            with Transformer() as transformer:
                transformed_record = transformer.transform(
                    record,
                    schema,
                    stream_metadata)
                write_record(stream_name, transformed_record, time_extracted=time_extracted)
                counter.increment()
        return counter.value


def sync_stream(stream_name, selected_streams, catalog, state, records):
    # Should sheets_loaded be synced?
    if stream_name in selected_streams:
        LOGGER.info('STARTED Syncing {}'.format(stream_name))
        update_currently_syncing(state, stream_name)
        write_schema(catalog, stream_name)
        record_count = process_records(
            catalog=catalog,
            stream_name=stream_name,
            records=records,
            time_extracted=utils.now())
        LOGGER.info('FINISHED Syncing {}, Total Records: {}'.format(stream_name, record_count))
        update_currently_syncing(state, None)


# Currently syncing sets the stream currently being delivered in the state.
# If the integration is interrupted, this state property is used to identify
#  the starting point to continue from.
# Reference: https://github.com/singer-io/singer-python/blob/master/singer/bookmarks.py#L41-L46
def update_currently_syncing(state, stream_name):
    if (stream_name is None) and ('currently_syncing' in state):
        del state['currently_syncing']
    else:
        singer.set_currently_syncing(state, stream_name)
    singer.write_state(state)


# List selected fields from stream catalog
def get_selected_fields(catalog, stream_name):
    stream = catalog.get_stream(stream_name)
    mdata = metadata.to_map(stream.metadata)
    mdata_list = singer.metadata.to_list(mdata)
    selected_fields = []
    for entry in mdata_list:
        field =  None
        try:
            field =  entry['breadcrumb'][1]
            if entry.get('metadata', {}).get('selected', False):
                selected_fields.append(field)
        except IndexError:
            pass
    return selected_fields


def get_data(stream_name,
             endpoint_config,
             client,
             spreadsheet_id,
             range_rows=None):
    if not range_rows:
        range_rows = ''
    path = endpoint_config.get('path', stream_name).replace(
        '{spreadsheet_id}', spreadsheet_id).replace('{sheet_title}', stream_name).replace(
            '{range_rows}', range_rows)
    params = endpoint_config.get('params', {})
    api = endpoint_config.get('api', 'sheets')
    querystring = '&'.join(['%s=%s' % (key, value) for (key, value) in params.items()]).replace(
        '{sheet_title}', stream_name)
    data = {}
    time_extracted = utils.now()
    data = client.get(
        path=path,
        api=api,
        params=querystring,
        endpoint=stream_name)
    return data, time_extracted


# Tranform file_metadata: remove nodes from lastModifyingUser, format as array
def transform_file_metadata(file_metadata):
    # Convert to dict
    file_metadata_tf = json.loads(json.dumps(file_metadata))
    # Remove keys
    if file_metadata_tf.get('lastModifyingUser'):
        file_metadata_tf['lastModifyingUser'].pop('photoLink', None)
        file_metadata_tf['lastModifyingUser'].pop('me', None)
        file_metadata_tf['lastModifyingUser'].pop('permissionId', None)
    # Add record to an array of 1
    file_metadata_arr = []
    file_metadata_arr.append(file_metadata_tf)
    return file_metadata_arr


# Tranform spreadsheet_metadata: remove defaultFormat and sheets nodes, format as array
def transform_spreadsheet_metadata(spreadsheet_metadata):
    # Convert to dict
    spreadsheet_metadata_tf = json.loads(json.dumps(spreadsheet_metadata))
    # Remove keys: defaultFormat and sheets (sheets will come in sheet_metadata)
    if spreadsheet_metadata_tf.get('properties'):
        spreadsheet_metadata_tf['properties'].pop('defaultFormat', None)
    spreadsheet_metadata_tf.pop('sheets', None)
    # Add record to an array of 1
    spreadsheet_metadata_arr = []
    spreadsheet_metadata_arr.append(spreadsheet_metadata_tf)
    return spreadsheet_metadata_arr


# Tranform spreadsheet_metadata: add spreadsheetId, sheetUrl, and columns metadata
def transform_sheet_metadata(spreadsheet_id, sheet, columns):
    # Convert to properties to dict
    sheet_metadata = sheet.get('properties')
    sheet_metadata_tf = json.loads(json.dumps(sheet_metadata)) 
    sheet_id = sheet_metadata_tf.get('sheetId')
    sheet_url = 'https://docs.google.com/spreadsheets/d/{}/edit#gid={}'.format(
        spreadsheet_id, sheet_id)
    sheet_metadata_tf['spreadsheetId'] = spreadsheet_id
    sheet_metadata_tf['sheetUrl'] = sheet_url
    sheet_metadata_tf['columns'] = columns
    return sheet_metadata_tf


# Convert Excel Date Serial Number (excel_date_sn) to datetime string
# timezone_str: defaults to UTC (which we assume is the timezone for ALL datetimes)
def excel_to_dttm_str(excel_date_sn, timezone_str=None):
    if not timezone_str:
        timezone_str = 'UTC'
    tz = pytz.timezone(timezone_str)
    sec_per_day = 86400
    excel_epoch = 25569 # 1970-01-01T00:00:00Z
    epoch_sec = math.floor((excel_date_sn - excel_epoch) * sec_per_day)
    epoch_dttm = datetime(1970, 1, 1)
    excel_dttm = epoch_dttm + timedelta(seconds=epoch_sec)
    utc_dttm = tz.localize(excel_dttm).astimezone(pytz.utc)
    utc_dttm_str = strftime(utc_dttm)
    return utc_dttm_str


# Transform sheet_data: add spreadsheet_id, sheet_id, and row, convert dates/times
#  Convert from array of values to JSON with column names as keys
def transform_sheet_data(spreadsheet_id, sheet_id, from_row, columns, sheet_data_rows):
    sheet_data_tf = []
    is_last_row = False
    row_num = from_row
    # Create sorted list of columns based on columnIndex
    cols = sorted(columns, key = lambda i: i['columnIndex'])

    # LOGGER.info('sheet_data_rows: {}'.format(sheet_data_rows))
    for row in sheet_data_rows:
        # If empty row, return sheet_data_tf w/ is_last_row and row_num - 1
        if row == []:
            is_last_row = True
            return sheet_data_tf, row_num - 1, is_last_row
        sheet_data_row_tf = {}
        # Add spreadsheet_id, sheet_id, and row
        sheet_data_row_tf['__sdc_spreadsheet_id'] = spreadsheet_id
        sheet_data_row_tf['__sdc_sheet_id'] = sheet_id
        sheet_data_row_tf['__sdc_row'] = row_num
        col_num = 1
        for value in row:
            # Select column metadata based on column index
            col = cols[col_num - 1]
            col_skipped = col.get('columnSkipped')
            if not col_skipped:
                col_name = col.get('columnName')
                col_type = col.get('columnType')
                # Convert dates/times from Lotus Notes Serial Numbers
                if col_type == 'numberType.DATE_TIME':
                    if isinstance(value, int) or isinstance(value, float):
                        col_val = excel_to_dttm_str(value)
                    else:
                        col_val = str(value)
                elif col_type == 'numberType.DATE':
                    if isinstance(value, int) or isinstance(value, float):
                        col_val = excel_to_dttm_str(value)[:10]
                    else:
                        col_val = str(value)
                elif col_type == 'numberType.TIME':
                    if isinstance(value, int) or isinstance(value, float):
                        try:
                            total_secs = value * 86400 # seconds in day
                            col_val = str(timedelta(seconds=total_secs))
                        except ValueError:
                            col_val = str(value)
                    else:
                        col_val = str(value)
                elif col_type == 'numberType':
                    if isinstance(value, int):
                        col_val = int(value)
                    else:
                        try:
                            col_val = float(value)
                        except ValueError:
                            col_val = str(value)
                elif col_type == 'stringValue':
                    col_val = str(value)
                elif col_type == 'boolValue':
                    if isinstance(value, bool):
                        col_val = value
                    elif isinstance(value, str):
                        if value.lower() in ('true', 't', 'yes', 'y'):
                            col_val = True
                        elif value.lower() in ('false', 'f', 'no', 'n'):
                            col_val = False
                        else:
                            col_val = str(value)
                    elif isinstance(value, int):
                        if value == 1 or value == -1:
                            col_val = True
                        elif value == 0:
                            col_val = False
                        else:
                            col_val = str(value)

                else:
                    col_val = value
                sheet_data_row_tf[col_name] = col_val
            col_num = col_num + 1
        sheet_data_tf.append(sheet_data_row_tf)
        row_num = row_num + 1
    return sheet_data_tf, row_num, is_last_row


def sync(client, config, catalog, state):
    start_date = config.get('start_date')
    spreadsheet_id = config.get('spreadsheet_id')

    # Get selected_streams from catalog, based on state last_stream
    #   last_stream = Previous currently synced stream, if the load was interrupted
    last_stream = singer.get_currently_syncing(state)
    LOGGER.info('last/currently syncing stream: {}'.format(last_stream))
    selected_streams = []
    for stream in catalog.get_selected_streams(state):
        selected_streams.append(stream.stream)
    LOGGER.info('selected_streams: {}'.format(selected_streams))

    if not selected_streams:
        return

    # FILE_METADATA
    file_metadata = {}
    stream_name = 'file_metadata'
    file_metadata_config = STREAMS.get(stream_name)

    # GET file_metadata
    LOGGER.info('GET file_meatadata')
    file_metadata, time_extracted = get_data(stream_name=stream_name,
                                             endpoint_config=file_metadata_config,
                                             client=client,
                                             spreadsheet_id=spreadsheet_id)
    # Transform file_metadata
    LOGGER.info('Transform file_meatadata')
    file_metadata_tf = transform_file_metadata(file_metadata)
    # LOGGER.info('file_metadata_tf = {}'.format(file_metadata_tf))

    # Check if file has changed, if not break (return to __init__)
    last_datetime = strptime_to_utc(get_bookmark(state, stream_name, start_date))
    this_datetime = strptime_to_utc(file_metadata.get('modifiedTime'))
    LOGGER.info('last_datetime = {}, this_datetime = {}'.format(last_datetime, this_datetime))
    if this_datetime <= last_datetime:
        LOGGER.info('this_datetime <= last_datetime, FILE NOT CHANGED. EXITING.')
        return 0
    else:
        # Sync file_metadata if selected
        sync_stream(stream_name, selected_streams, catalog, state, file_metadata_tf)
        write_bookmark(state, stream_name, strftime(this_datetime))

    # SPREADSHEET_METADATA
    spreadsheet_metadata = {}
    stream_name = 'spreadsheet_metadata'
    spreadsheet_metadata_config = STREAMS.get(stream_name)

    # GET spreadsheet_metadata
    LOGGER.info('GET spreadsheet_meatadata')
    spreadsheet_metadata, ss_time_extracted  = get_data(
        stream_name=stream_name,
        endpoint_config=spreadsheet_metadata_config,
        client=client,
        spreadsheet_id=spreadsheet_id)

    # Transform spreadsheet_metadata
    LOGGER.info('Transform spreadsheet_meatadata')
    spreadsheet_metadata_tf = transform_spreadsheet_metadata(spreadsheet_metadata)

    # Sync spreadsheet_metadata if selected
    sync_stream(stream_name, selected_streams, catalog, state, spreadsheet_metadata_tf)

    # SHEET_METADATA and SHEET_DATA
    sheets = spreadsheet_metadata.get('sheets')
    sheet_metadata = []
    sheets_loaded = []
    sheets_loaded_config = STREAMS['sheets_loaded']
    if sheets:
        # Loop thru sheets (worksheet tabs) in spreadsheet
        for sheet in sheets:
            sheet_title = sheet.get('properties', {}).get('title')
            sheet_id = sheet.get('properties', {}).get('sheetId')

            # GET sheet_metadata and columns
            sheet_schema, columns = get_sheet_metadata(sheet, spreadsheet_id, client)

            # Transform sheet_metadata
            sheet_metadata_tf = transform_sheet_metadata(spreadsheet_id, sheet, columns)
            # LOGGER.info('sheet_metadata_tf = {}'.format(sheet_metadata_tf))
            sheet_metadata.append(sheet_metadata_tf)

            # SHEET_DATA
            # Should this worksheet tab be synced?
            if sheet_title in selected_streams:
                LOGGER.info('STARTED Syncing Sheet {}'.format(sheet_title))
                update_currently_syncing(state, sheet_title)
                write_schema(catalog, sheet_title)

                # Determine max range of columns and rows for "paging" through the data
                sheet_last_col_index = 1
                sheet_last_col_letter = 'A'
                for col in columns:
                    col_index = col.get('columnIndex')
                    col_letter = col.get('columnLetter')
                    if col_index > sheet_last_col_index:
                        sheet_last_col_index = col_index
                        sheet_last_col_letter = col_letter
                sheet_max_row = sheet.get('properties').get('gridProperties', {}).get('rowCount')

                # Initialize paging for 1st batch
                is_last_row = False
                batch_rows = 200
                from_row = 2
                if sheet_max_row < batch_rows:
                    to_row = sheet_max_row
                else:
                    to_row = batch_rows

                # Loop thru batches (each having 200 rows of data)
                while not is_last_row and from_row < sheet_max_row and to_row <= sheet_max_row:
                    range_rows = 'A{}:{}{}'.format(from_row, sheet_last_col_letter, to_row)
                    
                    # GET sheet_data for a worksheet tab
                    sheet_data, time_extracted = get_data(
                        stream_name=sheet_title,
                        endpoint_config=sheets_loaded_config,
                        client=client,
                        spreadsheet_id=spreadsheet_id,
                        range_rows=range_rows)
                    # Data is returned as a list of arrays, an array of values for each row
                    sheet_data_rows = sheet_data.get('values')

                    # Transform batch of rows to JSON with keys for each column
                    sheet_data_tf, row_num, is_last_row = transform_sheet_data(
                        spreadsheet_id=spreadsheet_id,
                        sheet_id=sheet_id,
                        from_row=from_row,
                        columns=columns,
                        sheet_data_rows=sheet_data_rows)
                    if row_num < to_row:
                        is_last_row = True

                    # Process records, send batch of records to target
                    record_count = process_records(
                        catalog=catalog,
                        stream_name=sheet_title,
                        records=sheet_data_tf,
                        time_extracted=ss_time_extracted)
                    
                    # Update paging from/to_row for next batch
                    from_row = to_row + 1
                    if to_row + batch_rows > sheet_max_row:
                        to_row = sheet_max_row
                    else:
                        to_row = to_row + batch_rows

                # SHEETS_LOADED
                # Add sheet to sheets_loaded
                sheet_loaded = {}
                sheet_loaded['spreadsheetId'] = spreadsheet_id
                sheet_loaded['sheetId'] = sheet_id
                sheet_loaded['title'] = sheet_title
                sheet_loaded['loadDate'] = strftime(utils.now())
                sheet_loaded['lastRowNumber'] = row_num
                sheets_loaded.append(sheet_loaded)

                # Emit a Singer ACTIVATE_VERSION message after each sheet is complete.
                # This forces hard deletes on the data downstream if fewer records are sent for a sheet.
                # Reference: https://github.com/singer-io/singer-python/blob/9b99c6e0efc18836e6a07f1092aed8ba253f403f/singer/messages.py#L137-L167
                activate_version_message = singer.ActivateVersionMessage(
                    stream=sheet_title,
                    version=int(time.time() * 1000))
                singer.write_message(activate_version_message)

                LOGGER.info('FINISHED Syncing Sheet {}, Total Rows: {}'.format(
                    sheet_title, row_num - 1))
                update_currently_syncing(state, None)

    stream_name = 'sheet_metadata'
    # Sync sheet_metadata if selected
    sync_stream(stream_name, selected_streams, catalog, state, sheet_metadata)
    
    stream_name = 'sheets_loaded'
    # Sync sheet_metadata if selected
    sync_stream(stream_name, selected_streams, catalog, state, sheets_loaded)