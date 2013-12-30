#!/usr/bin/env python
#
# Deletes all indices with a datestamp older than "days-to-keep" for daily
# if you have hourly indices, it will delete all of those older than "hours-to-keep"
#
# This script presumes an index is named typically, e.g. logstash-YYYY.MM.DD
# It will work with any name-YYYY.MM.DD or name-YYYY.MM.DD.HH type sequence
#
# Requires python and the following dependencies (all pip/easy_installable):
#
# elasticsearch (official Elasticsearch Python API, http://www.elasticsearch.org/guide/en/elasticsearch/client/python-api/current/index.html)
# argparse (built-in in python2.7 and higher, python 2.6 and lower will have to easy_install it)
#
# TODO: Unit tests. The code is somewhat broken up into logical parts that may be tested separately.
#       Better error reporting?
#       Improve the get_index_epoch method to parse more date formats. Consider renaming (to "parse_date_to_timestamp"?)

import sys
import time
import logging
import argparse
from datetime import timedelta, datetime

import elasticsearch
from elasticsearch.exceptions import ElasticsearchException, ImproperlyConfigured

__version__ = '0.2.0'

def make_parser():
    """ Creates an ArgumentParser to parse the command line options. """
    parser = argparse.ArgumentParser(description='Delete old logstash indices from Elasticsearch.')

    parser.add_argument('-v', '--version', action='version', version='%(prog)s '+__version__)

    parser.add_argument('--host', help='Elasticsearch host.', default='localhost')
    parser.add_argument('--port', help='Elasticsearch port', default=9200, type=int)
    parser.add_argument('-t', '--timeout', help='Elasticsearch timeout', default=30, type=int)

    parser.add_argument('-p', '--prefix', help='Prefix for the indices. Indices that do not have this prefix are skipped.', default='logstash-')
    parser.add_argument('-s', '--separator', help='Time unit separator', default='.')

    parser.add_argument('-H', '--hours-to-keep', action='store', help='Number of hours to keep.', type=int)
    parser.add_argument('-d', '--days-to-keep', action='store', help='Number of days to keep.', type=int)
    parser.add_argument('-g', '--disk-space-to-keep', action='store', help='Disk space to keep (GB).', type=float)

    parser.add_argument('-n', '--dry-run', action='store_true', help='If true, does not perform any changes to the Elasticsearch indices.', default=False)
    parser.add_argument('-D', '--debug', dest='debug', action='store_true', help='Debug mode', default=False)
    parser.add_argument('-l', '--logfile', dest='log_file', help='log file', type=str, default=None)

    return parser


def get_index_epoch(index_timestamp, separator='.'):
    """ Gets the epoch of the index.

    :param index_timestamp: A string on the format YYYY.MM.DD[.HH]
    :return The creation time (epoch) of the index.
    """
    year_month_day_optionalhour = index_timestamp.split(separator)
    # If no hour has been appended, add UTC hour for "right now"
    # since Elasticsearch indices rollover at 00:00 UTC
    if len(year_month_day_optionalhour) == 3: 		
        year_month_day_optionalhour.append(datetime.utcnow().hour) 
    # Break down the parts on the separator
    t_array = [int(part) for part in year_month_day_optionalhour]
    # t_array: 0 = year, 1 = month, 2 = day, 3 = hour
    t_tuple = (t_array[0], t_array[1], t_array[2], t_array[3], 0, 0, 0, 0, 0)
    return time.mktime(t_tuple)


def find_expired_indices(IndicesClient, logger, days_to_keep=None, hours_to_keep=None, separator='.', prefix='logstash-', out=sys.stdout, err=sys.stderr):
    """ Generator that yields expired indices.

    :return: Yields tuples on the format ``(index_name, expired_by)`` where index_name
        is the name of the expired index and expired_by is the number of seconds (a float value) that the
        index was expired by.
    """
    utc_now_time = time.time() + 86400 # Add 1 day so we never prune the current index
    days_cutoff = utc_now_time - days_to_keep * 24 * 60 * 60 if days_to_keep is not None else None
    hours_cutoff = utc_now_time - hours_to_keep * 60 * 60 if hours_to_keep is not None else None

    try:
        sorted_indices = sorted(set(IndicesClient.get_settings().keys()))
    except (ImproperlyConfigured, ElasticsearchException, exception) as e:
        logger.exception(e)
        sys.exit(1)

    for index_name in sorted_indices:
        if not index_name.startswith(prefix):
            logger.debug('Skipping index due to missing prefix {0}: {1}'.format(prefix, index_name))
            continue

        unprefixed_index_name = index_name[len(prefix):]

        # find the timestamp parts (i.e ['2011', '01', '05'] from '2011.01.05') using the configured separator
        parts = unprefixed_index_name.split(separator)

        # perform some basic validation
        if len(parts) < 3 or len(parts) > 4 or not all([item.isdigit() for item in parts]):
            logger.error('Could not find a valid timestamp from the index: {0}'.format(index_name))
            continue

        # find the cutoff. if we have more than 3 parts in the timestamp, the timestamp includes the hours and we
        # should compare it to the hours_cutoff, otherwise, we should use the days_cutoff
        cutoff = hours_cutoff
        if len(parts) == 3:
            cutoff = days_cutoff

        # but the cutoff might be none, if the current index only has three parts (year.month.day) and we're only
        # removing hourly indices:
        if cutoff is None:
            logger.debug('Skipping {0} because it is of a type (hourly or daily) that I\'m not asked to delete.'.format(index_name))
            continue

        index_epoch = get_index_epoch(unprefixed_index_name)

        # if the index is older than the cutoff
        if index_epoch < cutoff:
            yield index_name, cutoff-index_epoch

        else:
            logger.info('{0} is {1} above the cutoff.'.format(index_name, timedelta(seconds=index_epoch-cutoff)))


def find_overusage_indices(IndicesClient, logger, disk_space_to_keep, separator='.', prefix='logstash-', out=sys.stdout, err=sys.stderr):
    """ Generator that yields over usage indices.

    :return: Yields tuples on the format ``(index_name, 0)`` where index_name
    is the name of the expired index. The second element is only here for
    compatiblity reasons.
    """

    disk_usage = 0.0
    disk_limit = disk_space_to_keep * 2**30

    try:
        sorted_indices = reversed(sorted(set(IndicesClient.get_settings().keys())))
    except (ImproperlyConfigured, ElasticsearchException, exception) as e:
        logger.exception(e)
        sys.exit(1)

    for index_name in sorted_indices:

        if not index_name.startswith(prefix):
            logger.debug('Skipping index due to missing prefix {0}: {1}'.format(prefix, index_name))
            continue

        index_size = IndicesClient.status(index_name)['indices'][index_name]['index']['primary_size_in_bytes']
        disk_usage += index_size

        if disk_usage > disk_limit:
            yield index_name, 0
        else:
            logger.info('keeping {0}, disk usage is {1:.3f} GB and disk limit is {2:.3f} GB.'.format(index_name, disk_usage/2**30, disk_limit/2**30))


def main():
    start = time.time()

    parser = make_parser()
    arguments = parser.parse_args()

    log_file = arguments.log_file if arguments.log_file else 'STDERR'

    # Setup logging
    logging.basicConfig(level=logging.DEBUG if arguments.debug else logging.INFO,
                        format='%(asctime)s %(levelname)-9s %(funcName)20s:%(lineno)-4d %(message)s',
                        datefmt="%Y-%m-%dT%H:%M:%S%z",
                        stream=open(arguments.log_file, 'a') if arguments.log_file else sys.stderr)
    logging.info("Job starting...")
    logger = logging.getLogger(__name__)

    # Setting up NullHandler to handle nested elasticsearch.trace Logger instance in elasticsearch python client
    h = logging.NullHandler()
    logging.getLogger('elasticsearch.trace').addHandler(h)

    if not arguments.hours_to_keep and not arguments.days_to_keep and not arguments.disk_space_to_keep:
        logger.error('Invalid arguments: You must specify either the number of hours, the number of days to keep or the maximum disk space to use')
        parser.print_help()
        return

    client = elasticsearch.Elasticsearch('{0}:{1}'.format(arguments.host, arguments.port), timeout=arguments.timeout)
    IndicesClient = elasticsearch.client.IndicesClient(client)

    if arguments.days_to_keep:
        logger.info('Deleting daily indices older than {0} days.'.format(arguments.days_to_keep))
        expired_indices = find_expired_indices(IndicesClient, logger, arguments.days_to_keep, arguments.hours_to_keep, arguments.separator, arguments.prefix)
    if arguments.hours_to_keep:
        logger.info('Deleting hourly indices older than {0} hours.'.format(arguments.hours_to_keep))
        expired_indices = find_expired_indices(IndicesClient, logger, arguments.days_to_keep, arguments.hours_to_keep, arguments.separator, arguments.prefix)
    if arguments.disk_space_to_keep:
        logger.info('Let\'s keep disk usage lower than {0} GB.'.format(arguments.disk_space_to_keep))
        expired_indices = find_overusage_indices(IndicesClient, logger, arguments.disk_space_to_keep, arguments.separator, arguments.prefix)


    for index_name, expired_by in expired_indices:
        expiration = timedelta(seconds=expired_by)

        if arguments.dry_run:
            logger.info('Would have attempted deleting index {0} because it is {1} older than the calculated cutoff.'.format(index_name, expiration))
            continue

        logger.info('Deleting index {0} because it was {1} older than cutoff.'.format(index_name, expiration))

        deletion = IndicesClient.delete(index_name)
        # ES returns a dict on the format {u'acknowledged': True, u'ok': True} on success.
        if deletion.get('ok'):
            logger.info('Successfully deleted index: {0}'.format(index_name))
        else:
            logger.error('Error deleting index: {0}. ({1})'.format(index_name, deletion))

    logger.info('Done in {0}.'.format(timedelta(seconds=time.time()-start)))


if __name__ == '__main__':
    main()
