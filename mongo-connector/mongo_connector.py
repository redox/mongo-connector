# Copyright 2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This file will be used with PyPi in order to package and distribute the final
# product.

"""Discovers the mongo cluster and starts the connector.
"""

import logging
import os
import re
import simplejson as json
import time
import sys
import inspect
import subprocess

#from doc_manager import DocManager
from pymongo import Connection
from oplog_manager import OplogThread
from optparse import OptionParser
from sys import exit
from threading import Thread
from util import bson_ts_to_long, long_to_bson_ts, retry_until_ok
from bson.timestamp import Timestamp


class Connector(Thread):
    """Checks the cluster for shards to tail.
    """
    def __init__(self, address, oplog_checkpoint, backend_url, ns_set,
                 u_key, auth_key, doc_manager=None, auth_username=None):
        file = inspect.getfile(inspect.currentframe())
        cmd_folder = os.path.realpath(os.path.abspath(os.path.split(file)[0]))
        if doc_manager is None:
            CMD = ["cp " + cmd_folder +
                   "/doc_managers/doc_manager_simulator.py "
                   + cmd_folder + "/doc_manager.py"]
            subprocess.Popen(CMD, shell=True)

        else:
            if doc_manager[0] is '/':
                CMD = ["cp " + doc_manager + " " +
                       + cmd_folder + "/doc_manager.py"]
                subprocess.Popen(CMD, shell=True)
            else:
                CMD = ["cp " + cmd_folder +
                   "/doc_managers/" + doc_manager + " " +
                   cmd_folder + "/doc_manager.py"]
                subprocess.Popen(CMD, shell=True)

        time.sleep(1)
        from doc_manager import DocManager
        super(Connector, self).__init__()

        #can_run is set to false when we join the thread
        self.can_run = True

        #The name of the file that stores the progress of the OplogThreads
        self.oplog_checkpoint = oplog_checkpoint

        #main address - either mongos for sharded setups or a primary otherwise
        self.address = address

        #The URL of the target system
        self.backend_url = backend_url

        "The set of relevant namespaces to consider
        self.ns_set = ns_set

        #The key that is a unique document identifier for the backend system.
        #Not necessarily the mongo unique key.
        self.u_key = u_key

        #Password for authentication
        self.auth_key = auth_key

        #Username for authentication
        self.auth_username = auth_username

        #The set of OplogThreads created
        self.shard_set = {}

        #Dict of OplogThread/timestmap pairs to record progress
        self.oplog_progress_dict = {}

        if backend_url is None:
            self.doc_manager = DocManager()
        else:
            self.doc_manager = DocManager(self.backend_url)

        if self.doc_manager is None:
            logging.critical('Bad backend URL!')
            return

    def join(self):
        """ Joins thread, stops it from running
        """
        self.can_run = False
        Thread.join(self)

    def write_oplog_progress(self):
        """ Writes oplog progress to file provided by user
        """

        if self.oplog_checkpoint is None:
                return None

        # write to temp file
        os.rename(self.oplog_checkpoint, self.oplog_checkpoint + '++')
        dest = open(self.oplog_checkpoint, 'w')
        source = open(self.oplog_checkpoint + '++', 'r')

        # for each of the threads write to file
        for oplog, ts in self.oplog_progress_dict.items():
            oplog_str = str(oplog)
            timestamp = bson_ts_to_long(ts)
            json_str = json.dumps([oplog_str, timestamp])
            dest.write(json_str)

        dest.close()
        source.close()
        os.remove(self.oplog_checkpoint + '++')

    def read_oplog_progress(self):
        """Reads oplog progress from file provided by user
        """

        if self.oplog_checkpoint is None:
            return None

        source = open(self.oplog_checkpoint, 'r')
        try:
            data = json.load(source)
        except json.decoder.JSONDecodeError:       # empty file
            logging.info('MongoConnector: Can't read oplog progress file. It may be empty or corrupt')
            return None

        count = 0
        for count in range(0, len(data), 2):
            oplog_str = data[count]
            ts = data[count + 1]
            self.oplog_progress_dict[oplog_str] = long_to_bson_ts(ts)
            #stored as bson_ts


    def run(self):
        """Discovers the mongo cluster and creates a thread for each primary.
        """
        main_conn = Connection(self.address)
        shard_coll = main_conn['config']['shards']

        self.read_oplog_progress()

        if shard_coll.find().count() == 0:
            #non sharded configuration

            oplog_coll = main_conn['local']['oplog.rs']
            print oplog_coll.find().count()
            if oplog_coll.find().count() == 0:
                err_msg = 'MongoConnector: No oplog for thread:'
                logging.info('%s %s' % (err_msg, main_conn))
                self.oplog_thread_join()
                self.doc_manager.auto_commit=False
                return

            oplog = OplogThread(main_conn, None, oplog_coll,
                                False, self.doc_manager,
                                self.oplog_progress_dict,
                                self.ns_set, self.auth_key,
                                self.auth_username)
            self.shard_set[0] = oplog
            logging.info('MongoConnector: Starting connection thread %s' %
                         shard_conn)
            oplog.start()

            while self.can_run:
                self.write_oplog_progress()
                time.sleep(1)

        else:       # sharded cluster
            while self.can_run is True:

                shard_cursor = shard_coll.find()

                for shard_doc in shard_cursor:
                    shard_id = shard_doc['_id']
                    if shard_id in self.shard_set:
                        self.write_oplog_progress()
                        time.sleep(1)
                        continue

                    repl_set, hosts = shard_doc['host'].split('/')
                    shard_conn = Connection(hosts, replicaset=repl_set)
                    oplog_coll = shard_conn['local']['oplog.rs']
                    oplog = OplogThread(shard_conn, self.address, oplog_coll,
                                        True, self.doc_manager,
                                        self.oplog_progress_dict,
                                        self.ns_set, self.auth_key,
                                        self.auth_username)
                    self.shard_set[shard_id] = oplog
                    logging.info('MongoConnector: Starting connection thread %s'
                                 % shard_conn)
                    oplog.start()

        self.oplog_thread_join()


    def oplog_thread_join(self):
        """Stops all the OplogThreads
        """
        logging.info('MongoConnector: Stopping all OplogThreads')
        for thread in self.shard_set.values():
            thread.join()

if __name__ == '__main__':
    """Runs mongo connector
    """

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    fh = logging.FileHandler('mongo_connector_log.txt')
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    logger.info('Beginning Mongo Connector')

    parser = OptionParser()

    #-m is for the main address, which is a host:port pair, ideally of the
    #mongos. For non sharded clusters, it can be the primary.
    parser.add_option("-m", "--main", action="store", type="string",
                      dest="main_addr", default="localhost:27217",
                      help="""Specify the mongos address, which is a """
                      """host:port pair, or for clusters with one shard,"""
                      """the primary's adderess. For example, `-m """
                      """localhost:27217` would be a valid argument"""
                      """to `-m`. It is not necessary to specify """
                      """double-quotes aroung the argument to `-m`. Don't """
                      """ use quotes around the address.""")

    #-o is to specify the oplog-config file. This file is used by the system
    #to store the last timestamp read on a specific oplog. This allows for
    #quick recovery from failure.
    parser.add_option("-o", "--oplog-ts", action="store", type="string",
                      dest="oplog_config", default="config.txt",
                      help="""Specify the name of the file that stores the"""
                      """oplog progress timestamps. """
                      """This file is used by the system to store the last"""
                      """timestamp read on a specific oplog. This allows"""
                      """ for quick recovery from failure. By default this"""
                      """ is `config.txt`, which starts off empty. An empty"""
                      """ file causes the system to go through all the mongo"""
                      """ oplog and sync all the documents. Whenever the """
                      """cluster is restarted, it is essential that the """
                      """oplog-timestamp config file be emptied - otherwise"""
                      """ the connector will miss some documents and behave"""
                      """incorrectly.""")

    #-b is to specify the URL to the backend engine being used.
    parser.add_option("-b", "--backend-url", action="store", type="string",
                      dest="url", default=None,
                      help="""Specify the URL to the backend engine being """
                      """used. For example, if you were using Solr out of """
                      """the box, you could use '-b """
                      """ http://localhost:8080/solr' with the """
                      """ SolrDocManager to establish a proper connection."""
                      """ Don't use quotes around address."""
                      """If target system doesn't need URL, don't specify""")

    #-n is to specify the namespaces we want to consider. The default
    #considers all the namespaces
    parser.add_option("-n", "--namespace-set", action="store", type="string",
                      dest="ns_set", default=None, help=
                      """Used to specify the namespaces we want to """
                      """ consider. For example, if we wished to store all """
                      """ documents from the test.test and alpha.foo """
                      """ namespaces, we could use `-n test.test,alpha.foo`."""
                      """ The default is to consider all the namespaces, """
                      """ excluding the system and config databases, and """
                      """ also ignoring the "system.indexes" collection in """
                      """any database.""")

    #-u is to specify the uniqueKey used by the backend,
    parser.add_option("-u", "--unique-key", action="store", type="string",
                      dest="u_key", default="_id", help=
                      """Used to specify the uniqueKey used by the backend."""
                      """The default is "_id", which can be noted by """
                      """  '-u _id'""")

    #-k is to specify the authentication key file. This file is used by mongos
    #to authenticate connections to the shards, and we'll use it in the oplog
    #threads.
    parser.add_option("-k", "--keyFile", action="store", type="string",
                      dest="auth_file", default=None, help=
                      """Used to specify the path to the authentication key"""
                      """file. This file is used by mongos to authenticate"""
                      """ connections to the shards, and we'll use it in the"""
                      """ oplog threads. If authentication is not used, then"""
                      """ this field can be left empty as the default """
                      """ is None.""")


    #-d is to specify the doc manager file.
    parser.add_option("-d", "--docManager", action="store", type="string",
                      dest="doc_manager", default=None, help=
                      """Used to specify the file in the /doc_managers"""
                      """folder that should be used as the doc manager."""
                      """Absolute paths also supported. By default, it will"""
                      """use the doc_manager_simulator.py file.""")

    #-a is to specify the username for authentication.
    parser.add_option("-a", "--admin-username", action="store", type="string",
                      dest="admin_name", default="__system", help=
                      """Used to specify the username of an admin user to"""
                      """authenticate with. To use authentication, the user"""
                      """must specify both an admin username and a keyFile."""
                      """The default username is '__system'""")

    (options, args) = parser.parse_args()

    try:
        if options.ns_set is None:
            ns_set = []
        else:
            ns_set = options.ns_set.split(',')
    except:
        logger.error('Namespaces must be separated by commas!')
        exit(1)

    key = None
    if options.auth_file is not None:
        try:
            file = open(options.auth_file)
            key = file.read()
            re.sub(r'\s', '', key)
        except:
            logger.error('Could not parse authentication file!')
            exit(1)

    ct = Connector(options.main_addr, options.oplog_config, options.url,
                   ns_set, options.u_key, key, option.doc_manager,
                   auth_username=options.admin_name)

    ct.run()