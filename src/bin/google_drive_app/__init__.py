"""
This module includes classes necessary to export and import lookups to Google spreadsheets.


from google_drive_app import GoogleLookupSync
google_lookup_sync = GoogleLookupSync('my_google_auth_file.json')
google_lookup_sync.import_to_lookup_file(lookup_name='some_lookup.csv', namespace='search', owner='nobody', google_spread_sheet_name='test_case_import', worksheet_name='data', session_key=session_key)

"""


import csv

import shutil
import logging
from . import lookupfiles

import os
import sys
import json
import shutil

from splunk.clilib.bundle_paths import make_splunkhome_path

# Prune directories from other apps so that we don't step on each other with our imports (see http://lukemurphey.net/issues/1281)
paths_to_remove = []
for path in sys.path:
    if ('/etc/apps/' in path and not '/etc/apps/google_drive' in path) or ('\\etc\\apps\\' in path and not '\\etc\\apps\\google_drive' in path):
        paths_to_remove.append(path)

for path in paths_to_remove:
    sys.path.remove(path)

# Remove the httplib2 library since this causes issues (https://lukemurphey.net/issues/2540)
try:
    shutil.rmtree(make_splunkhome_path(['etc', 'apps', 'google_drive', 'bin', 'google_drive_app', 'httplib2']))
except OSError:
    # The library doesn't exist; that's ok
    pass

# Add the imports
# Put the google_drive_app app first so that the app uses the newer version of requests which gspread expects
sys.path.insert(0, make_splunkhome_path(['etc', 'apps', 'google_drive', 'bin', 'google_drive_app']))
sys.path.append(make_splunkhome_path(['etc', 'apps', 'google_drive', 'bin', 'google_drive_app', 'oauth2client']))

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SERVICE_KEY_REALM = 'google_service_key'
SERVICE_KEY_USERNAME = 'GOOGLE_DRIVE_APP'

class SpreadsheetInaccessible(Exception):
    pass

class GoogleLookupSync(object):
    """
    This class performs operation for importing content from Google Drive to Splunk.
    """
    
    class OperationAction:
        OVERWRITE = 1
        APPEND    = 2
        
    class Operation:
        IMPORT      = "import"
        EXPORT      = "export"
        SYNCHRONIZE = "synchronize"
        
    def __init__(self, key_file=None, key_string=None, logger=None):
        self.gspread_client = None

        if key_file is not None:
            self.gspread_client = self.make_client(key_file)

        if self.gspread_client is None and key_string is not None:
            self.gspread_client = self.make_client_from_string(key_string)
    
        if self.gspread_client is None:
            raise ValueError("Gspread client was not constructed")

        self.logger = logger
        
        # Initialize a logger. This will cause it be initialized if one is not set yet.
        self.get_logger()
        
        #SPL-95681
        self.update_lookup_with_rest = True
        
    @classmethod
    def from_service_key_file(cls, key_file, logger=None):
        return GoogleLookupSync(key_file=key_file, logger=logger)

    @classmethod
    def from_service_key_string(cls, key_string, logger=None):
        return GoogleLookupSync(key_string=key_string, logger=logger)

    def make_client(self, key_file):
        """
        Authenticate to Google and initialize a gspread client.
        
        Args:
          key_file (str): The path to the key file
        """
        
        # Make sure the key was provided
        if key_file is None :
            raise ValueError("A key file must be provided")
        
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']

        credentials = ServiceAccountCredentials.from_json_keyfile_name(key_file, scope)
        
        return gspread.authorize(credentials)

    def make_client_from_string(self, key_string):
        """
        Authenticate to Google and initialize a gspread client.
        
        Args:
          key_string (str): a string containing the JSON of the key file
        """
        
        # Make sure the key was provided
        if key_string is None :
            raise ValueError("A key must be provided")

        # Parse the JSON
        key_json = json.loads(key_string)
        
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']

        credentials = ServiceAccountCredentials.from_json_keyfile_dict(key_json, scope)
        
        return gspread.authorize(credentials)

    
    def open_google_spreadsheet(self, title=None, key=None):
        """
        Open the spreadsheet with the given title or key, Either the key or title must be provided.
        
        If both are provided, then the title will be used. If a sheet could not be opened with the title, then the key will be used.
        
        Args:
          title (str, optional): The title of the document
          key (str, optional): The key of the document
          
        Returns:
          The Google spreadsheet object
        """
        
        try:
            if title is None and key is None:
                raise ValueError("You must supply either the title or the key of the sheet you want to open")
            
            google_spread_sheet = None
            
            # Try to open the file by the title
            if title is not None:
                google_spread_sheet = self.gspread_client.open(title)
            
            # If we don't have the sheet yet, try using the key
            if google_spread_sheet is None and key is not None:
                self.gspread_client.open_by_key(key)
            
            return google_spread_sheet
        except gspread.SpreadsheetNotFound:
            raise SpreadsheetInaccessible()
    
    def set_logger(self, logger):
        self.logger = logger
    
    def get_logger(self):
        """
        Setup a logger for this class.
        
        Returns:
          A logger
        """
    
        try:
            return logger
        except:
            pass
        
        if self.logger is not None:
            return self.logger
    
        logger = logging.getLogger('splunk.google_drive.GoogleLookupSync')
        logger.setLevel(logging.DEBUG)
        #logger.propagate = False # Prevent the log messages from being duplicated in the python.log file
    
        #file_handler = logging.handlers.RotatingFileHandler(make_splunkhome_path(['var', 'log', 'splunk', 'google_lookup_sync.log']), maxBytes=25000000, backupCount=5)
    
        #formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        #file_handler.setFormatter(formatter)
        #logger.addHandler(file_handler)
        
        self.logger = logger
        return logger
    
    def import_to_lookup_file_by_transform(self, lookup_transform, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, create_if_non_existent=False):
        """
        Import the spreadsheet from Google to the given lookup file.
        
        Args:
          lookup_transform (str): The name of the lookup file transform to write 
          namespace (str): 
          owner (str): 
          google_spread_sheet_name (str): 
          worksheet_name (str): 
          session_key (str): 
          create_if_non_existent (bool, optional): Defaults to False.
        """
        transform = lookupfiles.get_lookup_table_location(lookup_transform, namespace, owner, session_key, True)
        
        self.import_to_lookup_file(transform.filename, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, create_if_non_existent)
    
    def export_lookup_file(self, lookup_name, namespace, owner, google_spread_sheet_name, worksheet_name, session_key):
        """
        Export the spreadsheet from the given lookup file to Google.
        
        Args:
          lookup_name (str): The name of the lookup file to write (not the full path, just the stanza name)
          namespace (str): 
          owner (str): 
          google_spread_sheet_name (str): 
          worksheet_name (str): 
          session_key (str): 
        """
        
        splunk_lookup_table = lookupfiles.SplunkLookupTableFile.get(lookupfiles.SplunkLookupTableFile.build_id(lookup_name, namespace, owner), sessionKey=session_key)

        destination_full_path = splunk_lookup_table.path
        
        if namespace is None and splunk_lookup_table is not None:
            namespace = splunk_lookup_table.namespace
            
        if owner is None:
            owner = "nobody"
        
        if destination_full_path is None:
            raise Exception("Lookup file to export into does not exist")
        
        return self.export_lookup_file_full_path(destination_full_path, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, lookup_name=lookup_name)
    
    def get_worksheet_updated_date(self, google_spread_sheet_name, worksheet_name):
        """
       Get the date that the worksheet was last updated.
        
        Args:
          google_spread_sheet_name (str): 
          worksheet_name (str): 
        """
        
        try:
            google_spread_sheet = self.open_google_spreadsheet(google_spread_sheet_name)
            worksheet = google_spread_sheet.worksheet(worksheet_name)
            return worksheet.updated
        
        except gspread.WorksheetNotFound:
            return None
    
    def get_lookup_stats(self, lookup_full_path):
        """
        Get the row and column count for the lookup.

        Args:
          lookup_full_path (str): The full path of the file to export
        """

        col_count = 0
        row_count = 0

        with open(lookup_full_path, 'r') as file_handle:
            # Open the file
            csv_reader = csv.reader(file_handle)

            # Determine the column count
            col_count = len(next(csv_reader))
            file_handle.seek(0) # Go back to the first line

            # Determine the row count
            row_count = sum(1 for row in csv_reader)

        return col_count, row_count

    def export_lookup_file_full_path(self, lookup_full_path, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, lookup_name=None):
        """
        Export the spreadsheet from the given lookup file to Google.
        
        Args:
          lookup_full_path (str): The full path of the file to export
          namespace (str): 
          owner (str): 
          google_spread_sheet_name (str): 
          worksheet_name (str): 
          session_key (str):
          lookup_name (str): The name of the lookup file to export (not the full path, just the stanza name). This is necessary to use Splunk's safe method of copying.
        """
        
        # Open the spreadsheet
        google_spread_sheet = self.open_google_spreadsheet(google_spread_sheet_name)
        
        # Delete the worksheet since we will be re-creating it
        try:
            worksheet = google_spread_sheet.worksheet(worksheet_name)
            worksheet.clear()
            #google_spread_sheet.del_worksheet(worksheet)
        except gspread.WorksheetNotFound:
            pass #Spreadsheet did not exist. That's ok, we will make it.
        
        # Create the worksheet
        google_work_sheet = self.get_or_make_sheet_if_necessary(google_spread_sheet, worksheet_name)

        # Get the stats regaring the lookup
        col_count, row_count = self.get_lookup_stats(lookup_full_path)
        
        # Open the lookup file and export it
        with open(lookup_full_path, 'r') as file_handle:

            # Get the range of cells that we will be setting
            cell_list = worksheet.range(1, 1, row_count + 1, col_count)

            # Open the file
            csv_reader = csv.reader(file_handle)

            # Get an iterator for the spreadsheet
            cell_iter = iter(cell_list)

            # Write out the file
            for row in csv_reader:
                for cell in row:
                    # Update the next cell
                    next(cell_iter).value = cell
        
        # Write out the changes in batch
        google_work_sheet.update_cells(cell_list)
        
        # Log the result
        self.get_logger().info('Lookup exported successfully, user=%s, namespace=%s, lookup_file=%s', owner, namespace, lookup_name)
    
        # Get the new updated date
        worksheet = google_spread_sheet.worksheet(worksheet_name)
        return worksheet.updated
    
    def import_to_lookup_file(self, lookup_name, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, create_if_non_existent=False):
        """
        Import the spreadsheet from Google to the given lookup file.
        
        Args:
          lookup_name (str): The name of the lookup file to write (not the full path, just the stanza name)
          namespace (str): 
          owner (str): 
          google_spread_sheet_name (str): 
          worksheet_name (str): 
          session_key (str): 
          create_if_non_existent (bool, optional): Defaults to False.
        """
        
        splunk_lookup_table = lookupfiles.SplunkLookupTableFile.get(lookupfiles.SplunkLookupTableFile.build_id(lookup_name, namespace, owner), sessionKey=session_key)

        destination_full_path = splunk_lookup_table.path
        
        if namespace is None and splunk_lookup_table is not None:
            namespace = splunk_lookup_table.namespace
            
        if owner is None:
            owner = "nobody"
        
        if destination_full_path is None and not create_if_non_existent:
            raise Exception("Lookup file to import into does not exist")
        
        elif create_if_non_existent and destination_full_path is None:
            # TODO handle user-based lookups
            destination_full_path = make_splunkhome_path(['etc', 'apps', namespace, 'lookups', lookup_name])
        
        return self.import_to_lookup_file_full_path(destination_full_path, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, create_if_non_existent, lookup_name=lookup_name)
        
    def get_column_id(self, offset):
        """
        Get the ID of the column.
        """

        col_id = ''
        offset_left = offset

        while offset_left >= 0:
            next_val = offset_left % 26
            col_id = chr(65 + next_val) + col_id

            if offset_left == 0:
                offset_left = -1
            else:
                offset_left = (offset_left / 26) - 1

        return col_id

    def convert_to_dict(self, list_of_lists):
        l = []

        for row in list_of_lists:
            next_list = {}
            for index, value in enumerate(row):
                next_list[self.get_column_id(index)] = value

            l.append(next_list)

        return l

    def import_to_lookup_file_full_path(self, destination_full_path, namespace, owner, google_spread_sheet_name, worksheet_name, session_key, create_if_non_existent=False, lookup_name=None):
        """
        Import the spreadsheet from Google to the given lookup file.
        
        Args:
          destination_full_path (str): The full path of the file to write out
          namespace (str): 
          owner (str): 
          google_spread_sheet_name (str): 
          worksheet_name (str): 
          session_key (str):
          lookup_name (str): The name of the lookup file to write (not the full path, just the stanza name). This is necessary to use Splunk's safe method of copying.
          create_if_non_existent (bool, optional): Defaults to False.
        """
        
        # Open the spreadsheet
        google_spread_sheet = self.open_google_spreadsheet(google_spread_sheet_name)
        
        # Open or make the worksheet
        google_work_sheet = self.get_or_make_sheet_if_necessary(google_spread_sheet, worksheet_name)
        
        # Make a temporary lookup file
        temp_file_handle = lookupfiles.get_temporary_lookup_file()
        temp_file_name = temp_file_handle.name
        
        # Get the contents of spreadsheet and import it into the lookup
        list_of_lists = google_work_sheet.get_all_values()
        
        #with temp_file: #open(temp_file.name, 'w') as temp_file_handle:
        try:
            if temp_file_handle is not None and os.path.isfile(temp_file_name):
                
                # Open a CSV writer to edit the file
                csv_writer = csv.writer(temp_file_handle, lineterminator='\n')
                
                for row in list_of_lists:
                    
                    # Update the CSV with the row
                    csv_writer.writerow(row)
        
        finally:   
            if temp_file_handle is not None:
                temp_file_handle.close()
        
        # Determine if the lookup file exists, create it if it doesn't
        lookup_file_exists = os.path.exists(destination_full_path)
        
        if self.update_lookup_with_rest == False or not lookup_file_exists or lookup_name is None:
            
            # If we are not allowed to make the lookup file, then throw an exception
            if not lookup_file_exists and not create_if_non_existent:
                raise Exception("The lookup file to import the content to does not exist")
            
            # Manually copy the file to create it
            if lookup_file_exists:
                shutil.copy(temp_file_name, destination_full_path)
            else:
                shutil.move(temp_file_name, destination_full_path)
                
            # Log the result
            if not lookup_file_exists:
                self.get_logger().info('Lookup created successfully, user=%s, namespace=%s, lookup_file=%s', owner, namespace, lookup_name)
            else:
                self.get_logger().info('Lookup updated successfully, user=%s, namespace=%s, lookup_file=%s', owner, namespace, lookup_name)
    
            # If the file is new, then make sure that the list is reloaded so that the editors notice the change
            lookupfiles.SplunkLookupTableFile.reload()
            
        # Edit the existing lookup otherwise
        else:
            
            # Default to nobody if the owner is None
            if owner is None:
                owner = "nobody"
                
            if namespace is None:
                # Get the namespace from the lookup file entry if we don't know it already
                namespace = lookupfiles.SplunkLookupTableFile.get(lookupfiles.SplunkLookupTableFile.build_id(lookup_name, None, owner), sessionKey=session_key).namespace
                
            # Persist the changes from the temporary file
            lookupfiles.update_lookup_table(filename=temp_file_name, lookup_file=lookup_name, namespace=namespace, owner=owner, key=session_key)
    
            self.get_logger().info('Lookup updated successfully, user=%s, namespace=%s, lookup_file=%s', owner, namespace, lookup_name)
    
        # Get the new updated date
        worksheet = google_spread_sheet.worksheet(worksheet_name)
        return worksheet.updated
    
    def get_or_make_sheet_if_necessary(self, google_spread_sheet, worksheet_name, rows=100, cols=20):
        """
        Create the worksheet in the given Google document if it does not exist. If it does, return it. 
        
        Args:
          google_spread_sheet_name (str): 
          worksheet_name (str): 
          rows (int, optional): Defaults to 100.
          cols (int, optional): Defaults to 20.
          
        Returns:
          The Google worksheet object
        """
        
        try:
            worksheet = google_spread_sheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            # Worksheet was not found, make it
            worksheet = google_spread_sheet.add_worksheet(title=worksheet_name, rows=str(rows), cols=str(cols))
            
        return worksheet