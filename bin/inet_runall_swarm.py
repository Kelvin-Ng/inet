#!/usr/bin/env python3

import argparse
import os
import sys
import subprocess

import io
import zipfile

import time
import random
import pprint

import redis
import rq
import pymongo
import gridfs


REMOTE_INET_ROOT = "/opt/projects/inet-framework/inet"

LOCAL_INET_ROOT = os.path.abspath(
    os.path.dirname(os.path.realpath(__file__)) + "/..")

NED_PATH = REMOTE_INET_ROOT + "/src;" + REMOTE_INET_ROOT + "/examples;" + REMOTE_INET_ROOT + \
    "/showcases;" + REMOTE_INET_ROOT + "/tutorials;" + \
    REMOTE_INET_ROOT + "/tests/networks;."
INET_LIB = REMOTE_INET_ROOT + "/src/INET"

githash = ""


def local_inet_path_to_remote(local_path):
    return REMOTE_INET_ROOT + "/" + os.path.relpath(os.path.abspath(local_path), LOCAL_INET_ROOT)


DESCRIPTION = """\
Execute a number of OMNeT++ simulation runs on an INET Swarm application.
In the simplest case, you would invoke it like this:

% inet_runall_swarm -c Foo

where "Foo" is an omnetpp.ini configuration, containing iteration variables
and/or specifying multiple repetitions.

The first positional (non-option) argument and all following arguments are
treated as the simulation command (simulation program and its arguments).
Options intended for opp_runall should come before the the simulation command.

To limit the set of simulation runs to be performed, add a -r <runfilter>
argument to the simulation command.

Command-line options:
"""

EPILOG = """\
Operation: inet_runall_swarm invokes "./simprog -c Foo" with the "-q runnumbers"
extra command-line arguments to figure out how many (and which) simulation
runs it needs to perform, then ... magic happens. TODO
"""


def unzip_bytes(zip_bytes):
    with io.BytesIO(zip_bytes) as bytestream:
        with zipfile.ZipFile(bytestream, "r") as zipf:
            zipf.extractall(".")


class Runall:

    def __init__(self):
        print("connecting to the job queue")
        conn = redis.Redis(host="localhost")
        self.build_q = rq.Queue("build", connection=conn,
                                default_timeout=10 * 60 * 60)
        self.run_q = rq.Queue("run", connection=conn,
                              default_timeout=10 * 60 * 60)

    def run(self):
        opts = self.parseArgs()

        origwd = os.getcwd()
        if opts.directory != None:
            self.changeDir(opts.directory)

        run_numbers = self.resolveRunNumbers(opts.sim_prog_args)

        global githash

        start_time = time.perf_counter()
        print("queueing build job")

        build_job = self.build_q.enqueue("inet_worker.build_project", "inet-framework/inet", githash)

        print("waiting for build job to end")
        while not build_job.is_finished:
            if build_job.is_failed:
                build_job.refresh()
                print("ERROR: build job failed " + str(build_job.exc_info))
                exit(1)
            try:
                time.sleep(0.1)
            except KeyboardInterrupt:
                print("interrupted, not running tests (the build will still finish)")
                exit(1)

        end_time = time.perf_counter()

        print("build finished, took " + str(end_time -
                                            start_time) + "s, result:" + str(build_job.result))

        rng = random.SystemRandom()

        #rng.shuffle(run_numbers)

        run_jobs = []
        for rn in run_numbers:
            wd = os.getcwd()

            # if not wd.startswith('/'):
            wd = "/" + \
                os.path.relpath(os.path.abspath(os.getcwd()), LOCAL_INET_ROOT)

            # run the simulation
            workingdir = (REMOTE_INET_ROOT + "/" +
                          wd) if wd.startswith('/') else wd

            command = "opp_run_release -s -n '" + NED_PATH + "' -l " + INET_LIB + " -u Cmdenv " + \
                " ".join(opts.sim_prog_args) + " -r " + str(rn)

            runJob = self.run_q.enqueue(
                "inet_worker.run_simulation", "inet-framework/inet", githash, command, workingdir, depends_on=build_job)

            run_jobs.append(runJob)

        print("queued " + str(len(run_jobs)) + " jobs")

        stop = False
        while run_jobs and not stop:
            try:
                for j in run_jobs[:]:

                    if j.status is None:
                        print("Run " + "" + " was removed from the queue")
                        run_jobs.remove(j)
                    elif j.status == rq.job.JobStatus.FAILED:
                        j.refresh()
                        print("Run " + "" + " failed: " + str(j.exc_info))
                        run_jobs.remove(j)
                    else:
                        if j.result is not None:
                            pprint.pprint(j.result)

                            with pymongo.MongoClient("localhost", socketTimeoutMS=10 * 60 * 1000, connectTimeoutMS=10 * 60 * 1000, serverSelectionTimeoutMS=10 * 60 * 1000) as client:
                                gfs = gridfs.GridFS(client.opp)
                                unzip_bytes(gfs.get(j.id).read())

                            run_jobs.remove(j)

                time.sleep(1)
            except KeyboardInterrupt:
                stop = True

        if run_jobs:
            print("Cancelling " + str(len([j for j in run_jobs if j.status ==
                                           'queued' or j.status == 'deferred'])) + " pending jobs...")
            for j in run_jobs[:]:
                j.cancel()

        print("done")

        sys.exit(0)

    def parseArgs(self):
        currentGitRef = subprocess.check_output(
            ["git", "-C", LOCAL_INET_ROOT, "rev-parse", "HEAD"]).decode("utf-8")

        parser = argparse.ArgumentParser(
            description=DESCRIPTION, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument(
            '--commit', default=currentGitRef, metavar='commit')
        parser.add_argument('-C', '--directory', metavar='DIR',
                            help='Change to the given directory before doing anything.')

        try:
            opts, argv = parser.parse_known_args()
            opts.sim_prog_args = argv
        except IOError as e:
            print(e)
            exit(1)

        global githash
        githash = subprocess.check_output(
            ["git", "-C", LOCAL_INET_ROOT, "rev-parse", opts.commit.strip()]).decode("utf-8").strip()

        print("running with commit " + githash)

        return opts

    def changeDir(self, directory):
        print("changing into directory: " + directory)
        try:
            os.chdir(directory)
        except IOError as exc:
            print("Cannot change directory: " + str(exc))
            exit(1)

    def resolveRunNumbers(self, sim_prog_args):
        tmp_args = ["opp_run_dbg"] + sim_prog_args + ["-s", "-q", "runnumbers"]
        print("running: " + " ".join(tmp_args))
        try:
            output = subprocess.check_output(tmp_args)
        except subprocess.CalledProcessError as exc:
            print(
                sim_prog_args[0] + " [...] -q runnumbers returned nonzero exit status")
            exit(1)
        except IOError as exc:
            print("Cannot execute " +
                  sim_prog_args[0] + " [...] -q runnumbers: " + str(exc))
            exit(1)

        try:
            output = output.decode("utf-8")
            run_numbers = [int(num) for num in output.split()]
            print("run numbers: " + str(run_numbers))
            return run_numbers
        except:
            print("Error parsing output of " +
                  sim_prog_args[0] + " [...] -q runnumbers")
            exit(1)


tool = Runall()
tool.run()
