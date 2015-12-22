import subprocess, tempfile, json, os, shlex

from optparse import OptionParser, OptionGroup

import lnt.testing
import lnt.testing.util.compilers
from lnt.testing.util.misc import timestamp
from lnt.testing.util.commands import note, warning, fatal
from lnt.testing.util.commands import capture, mkdir_p, which
from lnt.testing.util.commands import resolve_command_path, isexecfile

from lnt.tests.builtintest import BuiltinTest

# This is the list of architectures in
# test-suite/cmake/modules/DetectArchitecture.cmake. If you update this list,
# make sure that cmake file is updated too.
TEST_SUITE_KNOWN_ARCHITECTURES = ['ARM', 'AArch64', 'Mips', 'X86']
KNOWN_SAMPLE_KEYS = ['compile', 'exec', 'hash']


class TestSuiteTest(BuiltinTest):
    def __init__(self):
        self.configured = False
        self.compiled = False

    def describe(self):
        return "LLVM test-suite"

    def run_test(self, name, args):
        # FIXME: Add more detailed usage information
        parser = OptionParser("%s [options] test-suite" % name)

        group = OptionGroup(parser, "Sandbox options")
        group.add_option("-S", "--sandbox", dest="sandbox_path",
                         help="Parent directory to build and run tests in",
                         type=str, default=None, metavar="PATH")
        group.add_option("", "--no-timestamp", dest="timestamp_build",
                         action="store_false", default=True,
                         help="Don't timestamp build directory (for testing)")
        group.add_option("", "--no-configure", dest="run_configure",
                         action="store_false", default=True,
                         help="Don't run CMake if CMakeCache.txt is present"
                              " (only useful with --no-timestamp")
        parser.add_option_group(group)
        
        group = OptionGroup(parser, "Inputs")
        group.add_option("", "--test-suite", dest="test_suite_root",
                         type=str, metavar="PATH", default=None,
                         help="Path to the LLVM test-suite sources")
        group.add_option("", "--test-externals", dest="test_suite_externals",
                         type=str, metavar="PATH",
                         help="Path to the LLVM test-suite externals")
        parser.add_option_group(group)
                         
        group = OptionGroup(parser, "Test compiler")
        group.add_option("", "--cc", dest="cc", metavar="CC",
                         type=str, default=None,
                         help="Path to the C compiler to test")
        group.add_option("", "--cxx", dest="cxx", metavar="CXX",
                         type=str, default=None,
                         help="Path to the C++ compiler to test (inferred from"
                              " --cc where possible")
        group.add_option("", "--llvm-arch", dest="llvm_arch",
                         type='choice', default=None,
                         help="Override the CMake-inferred architecture",
                         choices=TEST_SUITE_KNOWN_ARCHITECTURES)
        group.add_option("", "--cross-compiling", dest="cross_compiling",
                         action="store_true", default=False,
                         help="Inform CMake that it should be cross-compiling")
        group.add_option("", "--cross-compiling-system-name", type=str,
                         default=None, dest="cross_compiling_system_name",
                         help="The parameter to pass to CMAKE_SYSTEM_NAME when"
                              " cross-compiling. By default this is 'Linux' "
                              "unless -arch is in the cflags, in which case "
                              "it is 'Darwin'")
        group.add_option("", "--cppflags", type=str, action="append",
                         dest="cppflags", default=[],
                         help="Extra flags to pass the compiler in C or C++ mode. "
                              "Can be given multiple times")
        group.add_option("", "--cflags", type=str, action="append",
                         dest="cflags", default=[],
                         help="Extra CFLAGS to pass to the compiler. Can be "
                              "given multiple times")
        group.add_option("", "--cxxflags", type=str, action="append",
                         dest="cxxflags", default=[],
                         help="Extra CXXFLAGS to pass to the compiler. Can be "
                              "given multiple times")
        parser.add_option_group(group)

        group = OptionGroup(parser, "Test selection")
        group.add_option("", "--test-size", type='choice', dest="test_size",
                         choices=['small', 'regular', 'large'], default='regular',
                         help="The size of test inputs to use")
        group.add_option("", "--benchmarking-only",
                         dest="benchmarking_only", action="store_true",
                         default=False,
                         help="Benchmarking-only mode. Disable unit tests and "
                              "other flaky or short-running tests")
        group.add_option("", "--only-test", dest="only_test", metavar="PATH",
                         type=str, default=None,
                         help="Only run tests under PATH")

        parser.add_option_group(group)

        group = OptionGroup(parser, "Test Execution")
        group.add_option("-j", "--threads", dest="threads",
                         help="Number of testing threads",
                         type=int, default=1, metavar="N")
        group.add_option("", "--build-threads", dest="build_threads",
                         help="Number of compilation threads",
                         type=int, default=0, metavar="N")
        group.add_option("", "--use-perf", dest="use_perf",
                         help=("Use perf to obtain high accuracy timing"
                               "[%default]"),
                         type=str, default=None)
        group.add_option("", "--exec-multisample", dest="exec_multisample",
                         help="Accumulate execution test data from multiple runs",
                         type=int, default=1, metavar="N")
        group.add_option("", "--compile-multisample", dest="compile_multisample",
                         help="Accumulate compile test data from multiple runs",
                         type=int, default=1, metavar="N")

        parser.add_option_group(group)

        group = OptionGroup(parser, "Output Options")
        group.add_option("", "--submit", dest="submit_url", metavar="URLORPATH",
                         help=("autosubmit the test result to the given server"
                               " (or local instance) [%default]"),
                         type=str, default=None)
        group.add_option("", "--commit", dest="commit",
                         help=("whether the autosubmit result should be committed "
                                "[%default]"),
                          type=int, default=True)
        group.add_option("-v", "--verbose", dest="verbose",
                         help="show verbose test results",
                         action="store_true", default=False)
        group.add_option("", "--exclude-stat-from-submission",
                         dest="exclude_stat_from_submission",
                         help="Do not submit the stat of this type [%default]",
                         action='append', choices=KNOWN_SAMPLE_KEYS,
                         type='choice', default=['hash'])
        parser.add_option_group(group)

        group = OptionGroup(parser, "Test tools")
        group.add_option("", "--use-cmake", dest="cmake", metavar="PATH",
                         type=str, default="cmake",
                         help="Path to CMake [cmake]")
        group.add_option("", "--use-make", dest="make", metavar="PATH",
                         type=str, default="make",
                         help="Path to Make [make]")
        group.add_option("", "--use-lit", dest="lit", metavar="PATH",
                         type=str, default="llvm-lit",
                         help="Path to the LIT test runner [llvm-lit]")


        (opts, args) = parser.parse_args(args)
        self.opts = opts
        
        if args:
            parser.error("Expected no positional arguments (got: %r)" % (args,))

        for a in ['cross_compiling', 'cross_compiling_system_name', 'llvm_arch',
                  'benchmarking_only', 'use_perf']:
            if getattr(opts, a):
                parser.error('option "%s" is not yet implemented!' % a)
            
        if self.opts.sandbox_path is None:
            parser.error('--sandbox is required')

        # Option validation.
        opts.cc = resolve_command_path(opts.cc)

        if not lnt.testing.util.compilers.is_valid(opts.cc):
            parser.error('--cc does not point to a valid executable.')

        # If there was no --cxx given, attempt to infer it from the --cc.
        if opts.cxx is None:
            opts.cxx = lnt.testing.util.compilers.infer_cxx_compiler(opts.cc)
            if opts.cxx is not None:
                note("inferred C++ compiler under test as: %r" % (opts.cxx,))
            else:
                parser.error("unable to infer --cxx - set it manually.")

        if not os.path.exists(opts.cxx):
            parser.error("invalid --cxx argument %r, does not exist" % (opts.cxx))

        if opts.test_suite_root is None:
            parser.error('--test-suite is required')
        if not os.path.exists(opts.test_suite_root):
            parser.error("invalid --test-suite argument, does not exist: %r" % (
                opts.test_suite_root))

        if opts.test_suite_externals:
            if not os.path.exists(opts.test_suite_externals):
                parser.error(
                    "invalid --test-externals argument, does not exist: %r" % (
                        opts.test_suite_externals,))

        opts.cmake = resolve_command_path(opts.cmake)
        if not isexecfile(opts.cmake):
            parser.error("CMake tool not found (looked for %s)" % opts.cmake)
        opts.make = resolve_command_path(opts.make)
        if not isexecfile(opts.make):
            parser.error("Make tool not found (looked for %s)" % opts.make)
        opts.lit = resolve_command_path(opts.lit)
        if not isexecfile(opts.lit):
            parser.error("LIT tool not found (looked for %s)" % opts.lit)
                
        opts.cppflags = ' '.join(opts.cppflags)
        opts.cflags = ' '.join(opts.cflags)
        opts.cxxflags = ' '.join(opts.cxxflags)
        
        self.start_time = timestamp()

        # Work out where to put our build stuff
        if self.opts.timestamp_build:
            ts = self.start_time.replace(' ', '_').replace(':', '-')
            build_dir_name = "test-%s" % ts
        else:
            build_dir_name = "build"
        basedir = os.path.join(self.opts.sandbox_path, build_dir_name)
        self._base_path = basedir

        # We don't support compiling without testing as we can't get compile-
        # time numbers from LIT without running the tests.
        if opts.compile_multisample > opts.exec_multisample:
            note("Increasing number of execution samples to %d" %
                 opts.compile_multisample)
            opts.exec_multisample = opts.compile_multisample
        
        # Now do the actual run.
        reports = []
        for i in range(max(opts.exec_multisample, opts.compile_multisample)):
            c = i < opts.compile_multisample
            e = i < opts.exec_multisample
            reports.append(self.run("FIXME: nick", compile=c, test=e))
            
        report = self._create_merged_report(reports)

        # Write the report out so it can be read by the submission tool.
        report_path = os.path.join(self._base_path, 'report.json')
        with open(report_path, 'w') as fd:
            fd.write(report.render())

        return self.submit(report_path, self.opts, commit=True)
    
    def run(self, nick, compile=True, test=True):
        path = self._base_path
        
        if not os.path.exists(path):
            mkdir_p(path)
            
        if not self.configured and self._need_to_configure(path):
            self._configure(path)
            self._clean(path)
            self.configured = True
            
        if self.compiled and compile:
            self._clean(path)
        if not self.compiled or compile:
            self._make(path)
            self.compiled = True

        data = self._lit(path, test)
        return self._parse_lit_output(path, data)

    def _create_merged_report(self, reports):
        if len(reports) == 1:
            return reports[0]

        machine = reports[0].machine
        run = reports[0].run
        run.end_time = reports[-1].run.end_time
        test_samples = sum([r.tests for r in reports], [])
        return lnt.testing.Report(machine, run, test_samples)

    def _need_to_configure(self, path):
        cmakecache = os.path.join(path, 'CMakeCache.txt')
        return self.opts.run_configure or not os.path.exists(cmakecache)

    def _test_suite_dir(self):
        return self.opts.test_suite_root

    def _build_threads(self):
        return self.opts.build_threads or self.opts.threads

    def _test_threads(self):
        return self.opts.threads

    def _only_test(self):
        return self.opts.only_test

    def _clean(self, path):
        make_cmd = self.opts.make

        subdir = path
        if self._only_test():
            components = [path] + self._only_test().split('/')
            subdir = os.path.join(*components)

        subprocess.check_call([make_cmd, 'clean'],
                              cwd=subdir)
        
    def _configure(self, path):
        cmake_cmd = self.opts.cmake

        defs = {
            # FIXME: Support ARCH, SMALL/LARGE etc
            'CMAKE_C_COMPILER': self.opts.cc,
            'CMAKE_CXX_COMPILER': self.opts.cxx,
            'CMAKE_C_FLAGS': ' '.join([self.opts.cppflags, self.opts.cflags]),
            'CMAKE_CXX_FLAGS': ' '.join([self.opts.cppflags, self.opts.cxxflags])
        }

        subprocess.check_call([cmake_cmd, self._test_suite_dir()] +
                              ['-D%s=%s' % (k,v) for k,v in defs.items()],
                              cwd=path)

    def _make(self, path):
        make_cmd = self.opts.make
        
        subdir = path
        if self._only_test():
            components = [path] + self._only_test().split('/')
            subdir = os.path.join(*components)
        
        subprocess.check_call([make_cmd,
                               '-j', str(self._build_threads())],
                              cwd=subdir)

    def _lit(self, path, test):
        lit_cmd = self.opts.lit

        output_json_path = tempfile.NamedTemporaryFile(prefix='output',
                                                       suffix='.json',
                                                       dir=path,
                                                       delete=False)
        output_json_path.close()
        
        subdir = path
        if self._only_test():
            components = [path] + self._only_test().split('/')
            subdir = os.path.join(*components)

        extra_args = []
        if not test:
            extra_args = ['--no-execute']
        
        subprocess.check_call([lit_cmd,
                               '-sv',
                               '-j', str(self._test_threads()),
                               subdir,
                               '-o', output_json_path.name] + extra_args)

        return json.loads(open(output_json_path.name).read())

    def _is_pass_code(self, code):
        return code in ('PASS', 'XPASS', 'XFAIL')

    def _get_lnt_code(self, code):
        return {
            'PASS': lnt.testing.PASS,
            'FAIL': lnt.testing.FAIL,
            'XFAIL': lnt.testing.XFAIL,
            'XPASS': lnt.testing.FAIL,
            'UNRESOLVED': lnt.testing.FAIL
            }[code]
    
    def _test_failed_to_compile(self, raw_name, path):
        # FIXME: Do we need to add ".exe" in windows?
        name = raw_name.rsplit('.test', 1)[0]
        return not os.path.exists(os.path.join(path, name))

    def _get_target_flags(self):
        return shlex.split(self.opts.cppflags + self.opts.cflags)
    
    def _get_cc_info(self):
        return lnt.testing.util.compilers.get_cc_info(self.opts.cc,
                                                      self._get_target_flags())

    
    def _parse_lit_output(self, path, data, only_test=False):
        LIT_METRIC_TO_LNT = {
            'compile_time': 'compile',
            'exec_time': 'exec'
        }
        
        # We don't use the test info, currently.
        test_info = {}
        test_samples = []

        # FIXME: Populate with keys not to upload
        ignore = self.opts.exclude_stat_from_submission
        if only_test:
            ignore.append('compile')

        for test_data in data['tests']:
            raw_name = test_data['name'].split(' :: ', 1)[1]
            name = 'nts.' + raw_name.rsplit('.test', 1)[0]
            is_pass = self._is_pass_code(test_data['code'])
            if 'metrics' in test_data:
                for k,v in test_data['metrics'].items():
                    if k not in LIT_METRIC_TO_LNT or LIT_METRIC_TO_LNT[k] in ignore:
                        continue
                    test_samples.append(
                        lnt.testing.TestSamples(name + '.' + LIT_METRIC_TO_LNT[k],
                                                [v],
                                                test_info))

            if self._test_failed_to_compile(raw_name, path):
                test_samples.append(
                    lnt.testing.TestSamples(name + '.compile.status',
                                            [lnt.testing.FAIL],
                                            test_info))

            elif not is_pass:
                test_samples.append(
                    lnt.testing.TestSamples(name + '.exec.status',
                                            [self._get_lnt_code(test_data['code'])],
                                            test_info))


        # FIXME: Add more machine info!
        run_info = {
            'tag': 'nts'
        }
        run_info.update(self._get_cc_info())
        run_info['run_order'] = run_info['inferred_run_order']
        
        machine_info = {
        }
        
        machine = lnt.testing.Machine("jm", machine_info)
        run = lnt.testing.Run(self.start_time, timestamp(), info=run_info)
        report = lnt.testing.Report(machine, run, test_samples)
        return report
        
def create_instance():
    return TestSuiteTest()

__all__ = ['create_instance']