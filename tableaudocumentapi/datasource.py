import collections
import itertools
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sax
from uuid import uuid4

from tableaudocumentapi import Connection, xfile
from tableaudocumentapi import Field, DBColumn, Parameter, Relation, Extract
from tableaudocumentapi.multilookup_dict import MultiLookupDict
from tableaudocumentapi.xfile import xml_open


########
# This is needed in order to determine if something is a string or not.  It is necessary because
# of differences between python2 (basestring) and python3 (str).  If python2 support is ever
# dropped, remove this and change the basestring references below to str
try:
    basestring
except NameError:  # pragma: no cover
    basestring = str
########

_ColumnObjectReturnTuple = collections.namedtuple('_ColumnObjectReturnTupleType', ['id', 'object'])


def _get_metadata_xml_for_field(root_xml, field_name):
    if "'" in field_name:
        field_name = sax.escape(field_name, {"'": "&apos;"})
    xpath = u".//metadata-record[@class='column'][local-name='{}']".format(field_name)
    return root_xml.find(xpath)


def _is_used_by_worksheet(names, field):
    return any(y for y in names if y in field.worksheets)


class FieldDictionary(MultiLookupDict):

    def used_by_sheet(self, name):
        # If we pass in a string, no need to get complicated, just check to see if name is in
        # the field's list of worksheets
        if isinstance(name, basestring):
            return [x for x in self.values() if name in x.worksheets]

        # if we pass in a list, we need to check to see if any of the names in the list are in
        # the field's list of worksheets
        return [x for x in self.values() if _is_used_by_worksheet(name, x)]


def _column_object_from_column_xml(root_xml, column_xml):
    field_object = Field.from_column_xml(column_xml)
    local_name = field_object.id
    metadata_record = _get_metadata_xml_for_field(root_xml, local_name)
    if metadata_record is not None:
        field_object.apply_metadata(metadata_record)
    return _ColumnObjectReturnTuple(field_object.id, field_object)


def _column_object_from_metadata_xml(metadata_xml):
    field_object = Field.from_metadata_xml(metadata_xml)
    return _ColumnObjectReturnTuple(field_object.id, field_object)


def _db_column_object_from_db_column_xml(root_xml, column_xml):
    field_object = DBColumn.from_column_xml(column_xml)
    return _ColumnObjectReturnTuple(field_object._key, field_object)


def base36encode(number):
    """Converts an integer into a base36 string."""

    ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"

    base36 = ''
    sign = ''

    if number < 0:
        sign = '-'
        number = -number

    if 0 <= number < len(ALPHABET):
        return sign + ALPHABET[number]

    while number != 0:
        number, i = divmod(number, len(ALPHABET))
        base36 = ALPHABET[i] + base36

    return sign + base36


def _make_unique_name(dbclass):
    rand_part = base36encode(uuid4().int)
    name = dbclass + '.' + rand_part
    return name


class ParameterParser(object):
    """Parser for detecting and extracting parameters from a Tableau data source."""

    def __init__(self, datasource_xml, version):
        self._dsxml = datasource_xml
        self._dsversion = version

    def _extract_parameters(self):
        return list(map(Parameter, self._dsxml.findall('./datasource-dependencies[@datasource="Parameters"]/column')))

    def get_parameters(self):
        self._parameters = self._extract_parameters()
        return self._parameters


class ConnectionParser(object):
    """Parser for detecting and extracting connections from differing Tableau file formats."""

    def __init__(self, datasource_xml, version):
        self._dsxml = datasource_xml
        self._dsversion = version

    def _extract_federated_connections(self):
        connections = list(map(Connection, self._dsxml.findall('.//named-connections/named-connection/*')))
        # 'sqlproxy' connections (Tableau Server Connections) are not embedded into named-connection elements
        # extract them manually for now
        connections.extend(map(Connection, self._dsxml.findall(".//connection[@class='sqlproxy']")))
        return connections

    def _extract_legacy_connection(self):
        return list(map(Connection, self._dsxml.children('connection')))

    def get_connections(self):
        """Find and return all connections based on file format version."""

        if float(self._dsversion) < 10:
            connections = self._extract_legacy_connection()
        else:
            connections = self._extract_federated_connections()
        return connections


class Datasource(object):
    """A class representing Tableau Data Sources, embedded in workbook files or
    in TDS files.

    """

    def __init__(self, dsxml, filename=None):
        """
        Constructor.  Default is to create datasource from xml.

        """
        self._filename = filename
        self._datasourceXML = dsxml
        self._datasourceTree = ET.ElementTree(self._datasourceXML)
        self._name = self._datasourceXML.get('name') or self._datasourceXML.get(
            'formatted-name')  # TDS files don't have a name attribute
        self._version = self._datasourceXML.get('version')
        self._caption = self._datasourceXML.get('caption', '')
        self._connection_parser = ConnectionParser(self._datasourceXML, version=self._version)
        self._connections = self._connection_parser.get_connections()
        self._db_columns = self._get_db_column_objects()
        self._extract_columns = self._get_extract_column_objects()
        self._fields = None
        self._extract_fields = None
        self._parameter_parser = ParameterParser(self._datasourceXML, version=self._version)
        self._parameters = self._parameter_parser.get_parameters()
        self._columns = None

        self._relations = list(map(Relation, self._datasourceXML.findall("./connection[@class='federated']/relation")))
        self._extract = list(map(Extract, self._datasourceXML.findall("./extract")))

    @classmethod
    def from_file(cls, filename):
        """Initialize datasource from file (.tds ot .tdsx)"""

        dsxml = xml_open(filename, 'datasource').getroot()
        return cls(dsxml, filename)

    @classmethod
    def from_connections(cls, caption, connections):
        """Create a new Data Source give a list of Connections."""

        root = ET.Element('datasource', caption=caption, version='10.0', inline='true')
        outer_connection = ET.SubElement(root, 'connection')
        outer_connection.set('class', 'federated')
        named_conns = ET.SubElement(outer_connection, 'named-connections')
        for conn in connections:
            nc = ET.SubElement(named_conns,
                               'named-connection',
                               name=_make_unique_name(conn.dbclass),
                               caption=conn.server)
            nc.append(conn._connectionXML)
        return cls(root)

    def save(self):
        """
        Call finalization code and save file.

        Args:
            None.

        Returns:
            Nothing.

        """

        # save the file

        xfile._save_file(self._filename, self._datasourceTree)

    def save_as(self, new_filename):
        """
        Save our file with the name provided.

        Args:
            new_filename:  New name for the workbook file. String.

        Returns:
            Nothing.

        """

        xfile._save_file(self._filename, self._datasourceTree, new_filename)

    @property
    def name(self):
        return self._name

    @property
    def version(self):
        return self._version

    @property
    def caption(self):
        return self._caption

    @caption.setter
    def caption(self, value):
        self._datasourceXML.set('caption', value)
        self._caption = value

    @caption.deleter
    def caption(self):
        if 'caption' in self._datasourceXML.attrib:
            del self._datasourceXML.attrib['caption']
        self._caption = ''

    @property
    def connections(self):
        return self._connections

    def clear_repository_location(self):
        tag = self._datasourceXML.find('./repository-location')
        if tag is not None:
            self._datasourceXML.remove(tag)

    @property
    def fields(self):
        if not self._fields:
            self._fields = self._get_all_fields()
        return self._fields

    @property
    def extract_fields(self):
        if not self._extract_fields:
            self._extract_fields = self._get_extract_fields()
        return self._extract_fields

    @property
    def columns(self):
        if not self._columns:
            self._columns = self._get_column_objects()
        return self._columns

    @property
    def db_columns(self):
        if not self._db_columns:
            self._db_columns = self._get_db_column_objects()
        return self._db_columns

    @property
    def parameters(self):
        return self._parameters

    @property
    def extract(self):
        if not self.has_extract():
            return None
        return self._extract[0]

    def _get_all_fields(self):
        # Some columns are represented by `column` tags and others as `metadata-record` tags
        # Find them all and chain them into one dictionary
        column_field_objects = self.columns
        self._existing_column_fields = [x.id for x in column_field_objects]
        self._metadata_only_field_objects = (x for x in self._get_metadata_objects() if x.id not in self._existing_column_fields)
        field_objects = itertools.chain(column_field_objects, self._metadata_only_field_objects)

        return FieldDictionary({k: v for k, v in field_objects})

    def _get_extract_fields(self):
        self._extract_metadata_objects = (x for x in self._get_extract_metadata_objects())
        return FieldDictionary({k: v for k, v in self._extract_metadata_objects})

    def _get_metadata_objects(self):
        return (_column_object_from_metadata_xml(xml)
                for xml in self._datasourceTree.findall(".//metadata-record[@class='column']"))

    def _get_extract_metadata_objects(self):
        return (_column_object_from_metadata_xml(xml)
                for xml in self._datasourceTree.findall(".//extract/connection/metadata-records/metadata-record[@class='column']"))

    def _get_column_objects(self):
        return [_column_object_from_column_xml(self._datasourceTree, xml)
                for xml in self._datasourceTree.findall('./column')]

    def _get_db_column_objects(self):
        return dict([_db_column_object_from_db_column_xml(self._datasourceTree, xml)
                for xml in self._datasourceTree.findall('.//connection/cols/map')])

    def _get_extract_column_objects(self):
        return dict([_db_column_object_from_db_column_xml(self._datasourceTree, xml)
                for xml in self._datasourceTree.findall('.//extract/connection/cols/map')])

    def has_extract(self):
        return len(self._extract) > 0 and self._extract[0].enabled == 'true'

    def process_columns(self):
        sub_elems = self._datasourceTree.findall('*')
        last_aliases_index = -1
        first_column_index = -1
        for i in range(len(sub_elems)):
            if sub_elems[i].tag == 'column' and first_column_index < 0:
                first_column_index = i
            if sub_elems[i].tag == 'aliases':
                last_aliases_index = i
        column_index = max(first_column_index, last_aliases_index + 1)
        if column_index <= 0:
            raise LookupError("no column nor aliases element found in the data source")
        for name, field in self._fields.items():
            if field.id not in self._existing_column_fields:
                x = Field.create_field_xml(field.id, field.caption, field.datatype, field.role, field.type)
                Field.set_description(field.description, x)
                self._datasourceXML.insert(column_index, x)

    def get_query(self):
        return f"{self._relations[0]}"
