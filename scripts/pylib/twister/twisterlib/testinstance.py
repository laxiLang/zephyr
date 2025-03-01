# vim: set syntax=python ts=4 :
#
# Copyright (c) 2018-2022 Intel Corporation
# Copyright 2022 NXP
# SPDX-License-Identifier: Apache-2.0

import os
import hashlib
import random
import logging
import shutil
import glob

from twisterlib.testsuite import TestCase
from twisterlib.error import BuildError
from twisterlib.size_calc import SizeCalculator
from twisterlib.handlers import Handler, SimulationHandler, BinaryHandler, QEMUHandler, DeviceHandler, SUPPORTED_SIMS

logger = logging.getLogger('twister')
logger.setLevel(logging.DEBUG)

class TestInstance:
    """Class representing the execution of a particular TestSuite on a platform

    @param test The TestSuite object we want to build/execute
    @param platform Platform object that we want to build and run against
    @param base_outdir Base directory for all test results. The actual
        out directory used is <outdir>/<platform>/<test case name>
    """

    __test__ = False

    def __init__(self, testsuite, platform, outdir):

        self.testsuite = testsuite
        self.platform = platform

        self.status = None
        self.reason = "Unknown"
        self.metrics = dict()
        self.handler = None
        self.outdir = outdir
        self.execution_time = 0
        self.retries = 0

        self.name = os.path.join(platform.name, testsuite.name)
        self.run_id = self._get_run_id()
        self.build_dir = os.path.join(outdir, platform.name, testsuite.name)

        self.domains = None

        self.run = False
        self.testcases = []
        self.init_cases()
        self.filters = []
        self.filter_type = None

    def add_filter(self, reason, filter_type):
        self.filters.append({'type': filter_type, 'reason': reason })
        self.status = "filtered"
        self.reason = reason
        self.filter_type = filter_type

    # Fix an issue with copying objects from testsuite, need better solution.
    def init_cases(self):
        for c in self.testsuite.testcases:
            self.add_testcase(c.name, freeform=c.freeform)

    def _get_run_id(self):
        """ generate run id from instance unique identifier and a random
        number"""

        hash_object = hashlib.md5(self.name.encode())
        random_str = f"{random.getrandbits(64)}".encode()
        hash_object.update(random_str)
        return hash_object.hexdigest()

    def add_missing_case_status(self, status, reason=None):
        for case in self.testcases:
            if case.status == 'started':
                case.status = "failed"
            elif not case.status:
                case.status = status
                if reason:
                    case.reason = reason
                else:
                    case.reason = self.reason

    def __getstate__(self):
        d = self.__dict__.copy()
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)

    def __lt__(self, other):
        return self.name < other.name

    def set_case_status_by_name(self, name, status, reason=None):
        tc = self.get_case_or_create(name)
        tc.status = status
        if reason:
            tc.reason = reason
        return tc

    def add_testcase(self, name, freeform=False):
        tc = TestCase(name=name)
        tc.freeform = freeform
        self.testcases.append(tc)
        return tc

    def get_case_by_name(self, name):
        for c in self.testcases:
            if c.name == name:
                return c
        return None

    def get_case_or_create(self, name):
        for c in self.testcases:
            if c.name == name:
                return c

        logger.debug(f"Could not find a matching testcase for {name}")
        tc = TestCase(name=name)
        self.testcases.append(tc)
        return tc

    @staticmethod
    def testsuite_runnable(testsuite, fixtures):
        can_run = False
        # console harness allows us to run the test and capture data.
        if testsuite.harness in [ 'console', 'ztest', 'pytest', 'test', 'gtest', 'robot']:
            can_run = True
            # if we have a fixture that is also being supplied on the
            # command-line, then we need to run the test, not just build it.
            fixture = testsuite.harness_config.get('fixture')
            if fixture:
                can_run = fixture in fixtures

        return can_run

    def setup_handler(self, env):
        if self.handler:
            return

        options = env.options
        handler = Handler(self, "")
        if options.device_testing:
            handler = DeviceHandler(self, "device")
            handler.call_make_run = False
            handler.ready = True
        elif self.platform.simulation != "na":
            if self.platform.simulation == "qemu":
                handler = QEMUHandler(self, "qemu")
                handler.args.append(f"QEMU_PIPE={handler.get_fifo()}")
                handler.ready = True
            else:
                handler = SimulationHandler(self, self.platform.simulation)

            if self.platform.simulation_exec and shutil.which(self.platform.simulation_exec):
                handler.ready = True
        elif self.testsuite.type == "unit":
            handler = BinaryHandler(self, "unit")
            handler.binary = os.path.join(self.build_dir, "testbinary")
            if options.enable_coverage:
                handler.args.append("COVERAGE=1")
            handler.call_make_run = False
            handler.ready = True

        if handler:
            handler.options = options
            handler.generator_cmd = env.generator_cmd
            handler.generator = env.generator
            handler.suite_name_check = not options.disable_suite_name_check
        self.handler = handler

    # Global testsuite parameters
    def check_runnable(self, enable_slow=False, filter='buildable', fixtures=[]):

        # running on simulators is currently not supported on Windows
        if os.name == 'nt' and self.platform.simulation != 'na':
            return False

        # we asked for build-only on the command line
        if self.testsuite.build_only:
            return False

        # Do not run slow tests:
        skip_slow = self.testsuite.slow and not enable_slow
        if skip_slow:
            return False

        target_ready = bool(self.testsuite.type == "unit" or \
                        self.platform.type == "native" or \
                        self.platform.simulation in SUPPORTED_SIMS or \
                        filter == 'runnable')

        for sim in ['nsim', 'mdb-nsim', 'renode', 'tsim', 'native']:
            if self.platform.simulation == sim and self.platform.simulation_exec:
                if not shutil.which(self.platform.simulation_exec):
                    target_ready = False
                break
            else:
                target_ready = True


        testsuite_runnable = self.testsuite_runnable(self.testsuite, fixtures)

        return testsuite_runnable and target_ready

    def create_overlay(self, platform, enable_asan=False, enable_ubsan=False, enable_coverage=False, coverage_platform=[]):
        # Create this in a "twister/" subdirectory otherwise this
        # will pass this overlay to kconfig.py *twice* and kconfig.cmake
        # will silently give that second time precedence over any
        # --extra-args=CONFIG_*
        subdir = os.path.join(self.build_dir, "twister")

        content = ""

        if self.testsuite.extra_configs:
            new_config_list = []
            # some configs might be conditional on arch or platform, see if we
            # have a namespace defined and apply only if the namespace matches.
            # we currently support both arch: and platform:
            for config in self.testsuite.extra_configs:
                cond_config = config.split(":")
                if cond_config[0] == "arch" and len(cond_config) == 3:
                    if self.platform.arch == cond_config[1]:
                        new_config_list.append(cond_config[2])
                elif cond_config[0] == "plaform" and len(cond_config) == 3:
                    if self.platform.name == cond_config[1]:
                        new_config_list.append(cond_config[2])
                else:
                    new_config_list.append(config)

            content = "\n".join(new_config_list)

        if enable_coverage:
            if platform.name in coverage_platform:
                content = content + "\nCONFIG_COVERAGE=y"
                content = content + "\nCONFIG_COVERAGE_DUMP=y"

        if enable_asan:
            if platform.type == "native":
                content = content + "\nCONFIG_ASAN=y"

        if enable_ubsan:
            if platform.type == "native":
                content = content + "\nCONFIG_UBSAN=y"

        if content:
            os.makedirs(subdir, exist_ok=True)
            file = os.path.join(subdir, "testsuite_extra.conf")
            with open(file, "w") as f:
                f.write(content)

        return content

    def calculate_sizes(self, from_buildlog: bool = False, generate_warning: bool = True) -> SizeCalculator:
        """Get the RAM/ROM sizes of a test case.

        This can only be run after the instance has been executed by
        MakeGenerator, otherwise there won't be any binaries to measure.

        @return A SizeCalculator object
        """
        elf_filepath = self.get_elf_file()
        buildlog_filepath = self.get_buildlog_file() if from_buildlog else ''
        return SizeCalculator(elf_filename=elf_filepath,
                            extra_sections=self.testsuite.extra_sections,
                            buildlog_filepath=buildlog_filepath,
                            generate_warning=generate_warning)

    def get_elf_file(self) -> str:

        if self.testsuite.sysbuild:
            build_dir = self.domains.get_default_domain().build_dir
        else:
            build_dir = self.build_dir

        fns = glob.glob(os.path.join(build_dir, "zephyr", "*.elf"))
        fns.extend(glob.glob(os.path.join(build_dir, "zephyr", "*.exe")))
        fns.extend(glob.glob(os.path.join(build_dir, "testbinary")))
        blocklist = [
                'remapped', # used for xtensa plaforms
                'zefi', # EFI for Zephyr
                '_pre' ]
        fns = [x for x in fns if not any(bad in os.path.basename(x) for bad in blocklist)]
        if len(fns) != 1 and self.platform.type != 'native':
            raise BuildError("Missing/multiple output ELF binary")
        return fns[0]

    def get_buildlog_file(self) -> str:
        """Get path to build.log file.

        @raises BuildError: Incorrect amount (!=1) of build logs.
        @return: Path to build.log (str).
        """
        buildlog_paths = glob.glob(os.path.join(self.build_dir, "build.log"))
        if len(buildlog_paths) != 1:
            raise BuildError("Missing/multiple build.log file.")
        return buildlog_paths[0]

    def __repr__(self):
        return "<TestSuite %s on %s>" % (self.testsuite.name, self.platform.name)
