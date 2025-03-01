# SPDX-License-Identifier: Apache-2.0
from asyncio.log import logger
import re
import os
import subprocess
import shlex
from collections import OrderedDict
import xml.etree.ElementTree as ET
import logging
import time
import sys

logger = logging.getLogger('twister')
logger.setLevel(logging.DEBUG)

# pylint: disable=anomalous-backslash-in-string
result_re = re.compile(".*(PASS|FAIL|SKIP) - (test_)?(.*) in (\\d*[.,]?\\d*) seconds")
class Harness:
    GCOV_START = "GCOV_COVERAGE_DUMP_START"
    GCOV_END = "GCOV_COVERAGE_DUMP_END"
    FAULT = "ZEPHYR FATAL ERROR"
    RUN_PASSED = "PROJECT EXECUTION SUCCESSFUL"
    RUN_FAILED = "PROJECT EXECUTION FAILED"
    run_id_pattern = r"RunID: (?P<run_id>.*)"


    ztest_to_status = {
        'PASS': 'passed',
        'SKIP': 'skipped',
        'BLOCK': 'blocked',
        'FAIL': 'failed'
        }

    def __init__(self):
        self.state = None
        self.type = None
        self.regex = []
        self.matches = OrderedDict()
        self.ordered = True
        self.repeat = 1
        self.testcases = []
        self.id = None
        self.fail_on_fault = True
        self.fault = False
        self.capture_coverage = False
        self.next_pattern = 0
        self.record = None
        self.recording = []
        self.fieldnames = []
        self.ztest = False
        self.is_pytest = False
        self.detected_suite_names = []
        self.run_id = None
        self.matched_run_id = False
        self.run_id_exists = False
        self.instance = None
        self.testcase_output = ""
        self._match = False

    def configure(self, instance):
        self.instance = instance
        config = instance.testsuite.harness_config
        self.id = instance.testsuite.id
        self.run_id = instance.run_id
        if instance.testsuite.ignore_faults:
            self.fail_on_fault = False

        if config:
            self.type = config.get('type', None)
            self.regex = config.get('regex', [])
            self.repeat = config.get('repeat', 1)
            self.ordered = config.get('ordered', True)
            self.record = config.get('record', {})

    def process_test(self, line):

        runid_match = re.search(self.run_id_pattern, line)
        if runid_match:
            run_id = runid_match.group("run_id")
            self.run_id_exists = True
            if run_id == str(self.run_id):
                self.matched_run_id = True

        if self.RUN_PASSED in line:
            if self.fault:
                self.state = "failed"
            else:
                self.state = "passed"

        if self.RUN_FAILED in line:
            self.state = "failed"

        if self.fail_on_fault:
            if self.FAULT == line:
                self.fault = True

        if self.GCOV_START in line:
            self.capture_coverage = True
        elif self.GCOV_END in line:
            self.capture_coverage = False

class Robot(Harness):

    is_robot_test = True

    def configure(self, instance):
        super(Robot, self).configure(instance)
        self.instance = instance

        config = instance.testsuite.harness_config
        if config:
            self.path = config.get('robot_test_path', None)

    def handle(self, line):
        ''' Test cases that make use of this harness care about results given
            by Robot Framework which is called in run_robot_test(), so works of this
            handle is trying to give a PASS or FAIL to avoid timeout, nothing
            is writen into handler.log
        '''
        self.instance.state = "passed"
        tc = self.instance.get_case_or_create(self.id)
        tc.status = "passed"

    def run_robot_test(self, command, handler):

        start_time = time.time()
        env = os.environ.copy()
        env["ROBOT_FILES"] = self.path

        with subprocess.Popen(command, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=self.instance.build_dir, env=env) as cmake_proc:
            out, _ = cmake_proc.communicate()

            self.instance.execution_time = time.time() - start_time

            if cmake_proc.returncode == 0:
                self.instance.status = "passed"
            else:
                logger.error("Robot test failure: %s for %s" %
                             (handler.sourcedir, self.instance.platform.name))
                self.instance.status = "failed"

            if out:
                with open(os.path.join(self.instance.build_dir, handler.log), "wt") as log:
                    log_msg = out.decode(sys.getdefaultencoding())
                    log.write(log_msg)

class Console(Harness):

    def configure(self, instance):
        super(Console, self).configure(instance)
        if self.type == "one_line":
            self.pattern = re.compile(self.regex[0])
        elif self.type == "multi_line":
            self.patterns = []
            for r in self.regex:
                self.patterns.append(re.compile(r))

    def handle(self, line):
        if self.type == "one_line":
            if self.pattern.search(line):
                self.state = "passed"
        elif self.type == "multi_line" and self.ordered:
            if (self.next_pattern < len(self.patterns) and
                self.patterns[self.next_pattern].search(line)):
                self.next_pattern += 1
                if self.next_pattern >= len(self.patterns):
                    self.state = "passed"
        elif self.type == "multi_line" and not self.ordered:
            for i, pattern in enumerate(self.patterns):
                r = self.regex[i]
                if pattern.search(line) and not r in self.matches:
                    self.matches[r] = line
            if len(self.matches) == len(self.regex):
                self.state = "passed"
        else:
            logger.error("Unknown harness_config type")

        if self.fail_on_fault:
            if self.FAULT in line:
                self.fault = True

        if self.GCOV_START in line:
            self.capture_coverage = True
        elif self.GCOV_END in line:
            self.capture_coverage = False


        if self.record:
            pattern = re.compile(self.record.get("regex", ""))
            match = pattern.search(line)
            if match:
                csv = []
                if not self.fieldnames:
                    for k,v in match.groupdict().items():
                        self.fieldnames.append(k)

                for k,v in match.groupdict().items():
                    csv.append(v.strip())
                self.recording.append(csv)

        self.process_test(line)

        tc = self.instance.get_case_or_create(self.id)
        if self.state == "passed":
            tc.status = "passed"
        else:
            tc.status = "failed"

class Pytest(Harness):
    def configure(self, instance):
        super(Pytest, self).configure(instance)
        self.running_dir = instance.build_dir
        self.source_dir = instance.testsuite.source_dir
        self.pytest_root = 'pytest'
        self.pytest_args = []
        self.is_pytest = True
        config = instance.testsuite.harness_config

        if config:
            self.pytest_root = config.get('pytest_root', 'pytest')
            self.pytest_args = config.get('pytest_args', [])

    def handle(self, line):
        ''' Test cases that make use of pytest more care about results given
            by pytest tool which is called in pytest_run(), so works of this
            handle is trying to give a PASS or FAIL to avoid timeout, nothing
            is writen into handler.log
        '''
        self.state = "passed"
        tc = self.instance.get_case_or_create(self.id)
        tc.status = "passed"

    def pytest_run(self, log_file):
        ''' To keep artifacts of pytest in self.running_dir, pass this directory
            by "--cmdopt". On pytest end, add a command line option and provide
            the cmdopt through a fixture function
            If pytest harness report failure, twister will direct user to see
            handler.log, this method writes test result in handler.log
        '''
        cmd = [
			'pytest',
			'-s',
			os.path.join(self.source_dir, self.pytest_root),
			'--cmdopt',
			self.running_dir,
			'--junit-xml',
			os.path.join(self.running_dir, 'report.xml'),
			'-q'
        ]

        for arg in self.pytest_args:
            cmd.append(arg)

        log = open(log_file, "a")
        outs = []
        errs = []

        logger.debug(
                "Running pytest command: %s",
                " ".join(shlex.quote(a) for a in cmd))
        with subprocess.Popen(cmd,
                              stdout = subprocess.PIPE,
                              stderr = subprocess.PIPE) as proc:
            try:
                outs, errs = proc.communicate()
                tree = ET.parse(os.path.join(self.running_dir, "report.xml"))
                root = tree.getroot()
                for child in root:
                    if child.tag == 'testsuite':
                        if child.attrib['failures'] != '0':
                            self.state = "failed"
                        elif child.attrib['skipped'] != '0':
                            self.state = "skipped"
                        elif child.attrib['errors'] != '0':
                            self.state = "errors"
                        else:
                            self.state = "passed"
            except subprocess.TimeoutExpired:
                proc.kill()
                self.state = "failed"
            except ET.ParseError:
                self.state = "failed"
            except IOError:
                log.write("Can't access report.xml\n")
                self.state = "failed"

        tc = self.instance.get_case_or_create(self.id)
        if self.state == "passed":
            tc.status = "passed"
            log.write("Pytest cases passed\n")
        elif self.state == "skipped":
            tc.status = "skipped"
            log.write("Pytest cases skipped\n")
            log.write("Please refer report.xml for detail")
        else:
            tc.status = "failed"
            log.write("Pytest cases failed\n")

        log.write("\nOutput from pytest:\n")
        log.write(outs.decode('UTF-8'))
        log.write(errs.decode('UTF-8'))
        log.close()


class Gtest(Harness):
    ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    TEST_START_PATTERN = r"\[ RUN      \] (?P<suite_name>.*)\.(?P<test_name>.*)$"
    TEST_PASS_PATTERN = r"\[       OK \] (?P<suite_name>.*)\.(?P<test_name>.*)$"
    TEST_FAIL_PATTERN = r"\[  FAILED  \] (?P<suite_name>.*)\.(?P<test_name>.*)$"
    FINISHED_PATTERN = r"\[==========\] Done running all tests\.$"
    has_failures = False
    tc = None

    def handle(self, line):
        # Strip the ANSI characters, they mess up the patterns
        non_ansi_line = self.ANSI_ESCAPE.sub('', line)

        # Check if we started running a new test
        test_start_match = re.search(self.TEST_START_PATTERN, non_ansi_line)
        if test_start_match:
            # Add the suite name
            suite_name = test_start_match.group("suite_name")
            if suite_name not in self.detected_suite_names:
                self.detected_suite_names.append(suite_name)

            # Generate the internal name of the test
            name = "{}.{}.{}".format(self.id, suite_name, test_start_match.group("test_name"))

            # Assert that we don't already have a running test
            assert (
                self.tc is None
            ), "gTest error, {} didn't finish".format(self.tc)

            # Check that the instance doesn't exist yet (prevents re-running)
            tc = self.instance.get_case_by_name(name)
            assert tc is None, "gTest error, {} running twice".format(tc)

            # Create the test instance and set the context
            tc = self.instance.get_case_or_create(name)
            self.tc = tc
            self.tc.status = "started"
            self.testcase_output += line + "\n"
            self._match = True

        # Check if the test run finished
        finished_match = re.search(self.FINISHED_PATTERN, non_ansi_line)
        if finished_match:
            tc = self.instance.get_case_or_create(self.id)
            if self.has_failures or self.tc is not None:
                self.state = "failed"
                tc.status = "failed"
            else:
                self.state = "passed"
                tc.status = "passed"
            return

        # Check if the individual test finished
        state, name = self._check_result(non_ansi_line)
        if state is None or name is None:
            # Nothing finished, keep processing lines
            return

        # Get the matching test and make sure it's the same as the current context
        tc = self.instance.get_case_by_name(name)
        assert (
            tc is not None and tc == self.tc
        ), "gTest error, mismatched tests. Expected {} but got {}".format(self.tc, tc)

        # Test finished, clear the context
        self.tc = None

        # Update the status of the test
        tc.status = state
        if tc.status == "failed":
            self.has_failures = True
            tc.output = self.testcase_output
        self.testcase_output = ""
        self._match = False

    def _check_result(self, line):
        test_pass_match = re.search(self.TEST_PASS_PATTERN, line)
        if test_pass_match:
            return "passed", "{}.{}.{}".format(self.id, test_pass_match.group("suite_name"), test_pass_match.group("test_name"))
        test_fail_match = re.search(self.TEST_FAIL_PATTERN, line)
        if test_fail_match:
            return "failed", "{}.{}.{}".format(self.id, test_fail_match.group("suite_name"), test_fail_match.group("test_name"))
        return None, None


class Test(Harness):
    RUN_PASSED = "PROJECT EXECUTION SUCCESSFUL"
    RUN_FAILED = "PROJECT EXECUTION FAILED"
    test_suite_start_pattern = r"Running TESTSUITE (?P<suite_name>.*)"
    ZTEST_START_PATTERN = r"START - (test_)?(.*)"

    def handle(self, line):
        test_suite_match = re.search(self.test_suite_start_pattern, line)
        if test_suite_match:
            suite_name = test_suite_match.group("suite_name")
            self.detected_suite_names.append(suite_name)

        testcase_match = re.search(self.ZTEST_START_PATTERN, line)
        if testcase_match:
            name = "{}.{}".format(self.id, testcase_match.group(2))
            tc = self.instance.get_case_or_create(name)
            # Mark the test as started, if something happens here, it is mostly
            # due to this tests, for example timeout. This should in this case
            # be marked as failed and not blocked (not run).
            tc.status = "started"

        if testcase_match or self._match:
            self.testcase_output += line + "\n"
            self._match = True

        result_match = result_re.match(line)

        if result_match and result_match.group(2):
            matched_status = result_match.group(1)
            name = "{}.{}".format(self.id, result_match.group(3))
            tc = self.instance.get_case_or_create(name)
            tc.status = self.ztest_to_status[matched_status]
            if tc.status == "skipped":
                tc.reason = "ztest skip"
            tc.duration = float(result_match.group(4))
            if tc.status == "failed":
                tc.output = self.testcase_output
            self.testcase_output = ""
            self._match = False
            self.ztest = True

        self.process_test(line)

        if not self.ztest and self.state:
            logger.debug(f"not a ztest and no state for  {self.id}")
            tc = self.instance.get_case_or_create(self.id)
            if self.state == "passed":
                tc.status = "passed"
            else:
                tc.status = "failed"

class Ztest(Test):
    pass
