import argparse
import codecs
import dataclasses
import json
import logging
import os
import requests
import shutil
import subprocess
import typing
import re
import sys

logger = logging.getLogger()


@dataclasses.dataclass(unsafe_hash=True, order=True)
class Dependency:
    package: str = ''
    installed: str = ''
    requires: typing.Tuple[typing.Tuple[str, str]] = None
    conflicts: typing.Tuple[typing.Tuple[str, str]] = None

    def __str__(self):
        total = self.requires + self.conflicts
        parts = (''.join(part) for part in total)
        return '%s %s' % (self.package, ','.join(parts))

    @classmethod
    def _requires(cls, requirement) -> tuple:
        requires, conflicts = [], []

        if not requirement:
            return (), ()

        # Requirement could be multiple.
        for part in requirement.split(','):
            # Separate operation and version.
            search = re.search(r'^([<>!=]*)(.*)$', part)
            part_op, part_ver = search.groups()

            # RPM does not support != in 'Requires' tag.
            # Instead RPM supports 'Conflicts' tag.
            if part_op != '!=':
                requires.append((part_op, part_ver))
            else:
                conflicts.append((part_op, part_ver))

        # This entities must be hashable.
        return tuple(requires), tuple(conflicts)

    @classmethod
    def parse(cls, dep: dict):
        package, installed = dep['package_name'], dep['installed_version']
        requires, conflicts = cls._requires(dep['required_version'])
        return cls(package, installed, requires, conflicts)


@dataclasses.dataclass
class Package:
    package: str = ''
    version: str = ''
    archive: str = ''
    sources: str = ''
    own_deps: typing.List[Dependency] = None
    all_deps: typing.List[Dependency] = None

    @property
    def expression(self):
        if re.match(r'^[<>=]+', self.version):
            return '%s%s' % (self.package, self.version)
        elif self.version:
            return '%s==%s' % (self.package, self.version)
        else:
            return self.package


class Packetizer:
    def __init__(self, python_path: str, names_prefix: str, index_url: str, json_url: str):
        # Python used for RPM building hard-coded into spec.
        # Python used for package downloading and installing.
        self.system_python = self.active_python = python_path

        # RPM package names prefix (also for deps).
        self.prefix = names_prefix

        # Pip API endpoints.
        self.index_url = index_url
        self.json_url = json_url

        # Where to create archives and build specs.
        self.temp = os.path.expanduser('~/rpmbuild/PYTHON/temp')
        self.venv = os.path.expanduser('~/rpmbuild/PYTHON/venv')

        # Where to put created archives and specs.web/pads/api_v2/mediation.py
        self.sources = os.path.expanduser('~/rpmbuild/SOURCES')
        self.specs = os.path.expanduser('~/rpmbuild/SPECS')

        if not os.path.exists(self.temp):
            logger.info('Creating temporary: %s', self.temp)
            os.makedirs(self.temp)

        if not os.path.exists(self.venv):
            logger.info('Creating virtualenv: %s', self.venv)
            check_output([self.active_python, '-m', 'venv', self.venv])

            logger.info('Activating virtualenv: %s', self.venv)
            self.active_python = os.path.join(self.venv, 'bin/python')

            logger.info('Installing pipdeptree: %s', 'pipdeptree==0.13.2')
            check_output([self.active_python, '-m', 'pip', 'install',
                          '-i', self.index_url, 'pipdeptree==0.13.2'])
        else:
            logger.info('Activating virtualenv: %s', self.venv)
            self.active_python = os.path.join(self.venv, 'bin/python')

        os.makedirs(self.sources, exist_ok=True)
        os.makedirs(self.specs, exist_ok=True)

    def packetize(self, package: str, verexpr: str, recursive: bool, exclude: str):
        logger.info('Building RPM spec for %s%s', package, verexpr)
        package = Package(package=package, version=verexpr)

        # Install requested package into virtualenv.
        self._install_package_to_venv(package)

        # Get information about installed package.
        self._collect_package_metadata(package)

        # Download installed package sources.
        self._download_package_sources(package)

        # Build rpm package from downloaded archive.
        self._build_package_spec(package)

        # Build also all package dependencies.
        # All dependencies installed in prepare phase.
        if recursive:
            installed = sorted({(d.package, d.installed) for d in package.all_deps})
            for depname, version in installed:
                if exclude and re.search(exclude, depname, re.IGNORECASE):
                    continue

                logger.info('Building RPMs for %s==%s', depname, version)
                dependent = Package(package=depname, version=version)

                # Get information about installed package.
                self._collect_package_metadata(dependent)

                # Download installed package sources.
                self._download_package_sources(dependent)

                # Build rpm package from downloaded archive.
                self._build_package_spec(dependent)

    def _parse_deps_tree(self, package: Package, deps: list):
        """
            Reads dependencies tree and fills package.
        """
        # Find current package in all packages output.
        own_deps = []
        for entry in deps:
            if entry['package']['package_name'] == package.package:
                for dependency in entry['dependencies']:
                    own_deps.append(dependency)

        # Flatify nested dependency structure.
        all_deps, queue = [], own_deps[:]
        while queue:
            next_entry = queue.pop(-1)
            all_deps.append(next_entry)

            for entry in deps:
                if entry['package']['package_name'] == next_entry['package_name']:
                    queue.extend(entry['dependencies'])

        # Make result sorted and unique.
        package.own_deps = sorted({Dependency.parse(dep) for dep in own_deps})
        package.all_deps = sorted({Dependency.parse(dep) for dep in all_deps})

    def _patch_spec_data(self, package: Package, lines: list):
        """
            Changes SPEC file content.
        """
        result = [
            # Python path used for packages installing.
            # This macros also used at /usr/lib/rpm macroses.
            '%%define __python %s\n' % self.system_python,

            # Package name before manipulations.
            # Matches directory name in sources archive.
            '%%define original_name %s\n' % package.package,
        ]

        for line in lines:
            if '%define name' in line:
                # Name macro will contains full name with prefix.
                result.append('%%define name %s%s\n' % (self.prefix, package.package))

            elif '%description' in line:
                # Package dependencies gets prefix.
                for dependency in package.own_deps:
                    # Get information about installed package.
                    deppackage = Package(dependency.package)
                    self._collect_package_metadata(deppackage)

                    fullname = '%s%s' % (self.prefix, deppackage.package)
                    if not dependency.requires and not dependency.conflicts:
                        result.append('Requires: %s\n' % fullname)
                    else:
                        for require in dependency.requires:
                            result.append('Requires: %s %s %s\n' % (fullname, require[0], require[1]))
                        for conflict in dependency.conflicts:
                            result.append('Conflicts: %s == %s\n' % (fullname, conflict[1]))

                # Dependencies goes before description.
                result.append(line)

            elif '%setup' in line:
                result.append('%setup -n %{original_name}-%{unmangled_version}')

            elif 'Source0:' in line:
                # Package archive may be in .tar.gz, in .zip, in .tar.xz.
                # But setuptools bdist_rpm writes .tar.gz suffix.
                result.append('Source0: %s\n' % os.path.basename(package.archive))

            else:
                result.append(line)
        return result

    def _install_package_to_venv(self, package: Package):
        """
            Installs pip package expression into virtual environment.
        """
        logger.info('Installing package: %s%s...', package.package, package.version)
        check_output([self.active_python, '-m', 'pip', 'install',
                      '-i', self.index_url, package.expression])

    def _collect_package_metadata(self, package: Package):
        """
            Executes pip show to detect package name and version.
            Executes pipdeptree to detect package dependencies.
        """
        logger.info('Querying version: %s...', package.package)
        output = check_output([self.active_python, '-m', 'pip', 'show', package.package])
        package.package = re.search(r'Name: ([^\s]+)', output).group(1)
        package.version = re.search(r'Version: ([^\s]+)', output).group(1)
        logger.info('Querying version: %s installed', package.expression)

        logger.info('Querying dependencies: %s...', package.expression)
        output = check_output([self.active_python, '-m', 'pipdeptree', '--json'])
        self._parse_deps_tree(package, json.loads(output))
        for dependency in package.own_deps:
            logger.info('Querying dependencies: own: %s', dependency)
        for dependency in package.all_deps:
            logger.info('Querying dependencies: all: %s', dependency)

    def _download_package_sources(self, package: Package):
        """
            Downloads installed package version sources archive.
        """
        logger.info('Querying sources: %s...', package.expression)
        output = check_output([self.active_python, '-m', 'pip', 'show', package.package])
        package.package = re.search(r'Name: ([^\s]+)', output).group(1)
        package.version = re.search(r'Version: ([^\s]+)', output).group(1)

        data = requests.get(self.json_url % package.package).json()
        meta = next(d for d in data['releases'][package.version] if d['packagetype'] == 'sdist')
        logger.info('Querying sources: %s found', meta['url'])

        logger.info('Downloading sources: %s', package.package)
        content = requests.get(meta['url']).content
        target = os.path.join(self.sources, meta['filename'])
        with open(target, 'wb') as fp:
            fp.write(content)
        package.archive = target
        logger.info('Downloading sources: %s downloaded', package.archive)

        logger.info('Unpacking sources: %s...', package.archive)
        shutil.unpack_archive(os.path.join(self.sources, package.archive), self.temp)
        package.sources = os.path.join(self.temp, '%s-%s' % (package.package, package.version))
        logger.info('Unpacking sources: %s unpacked', package.sources)

    def _build_package_spec(self, package: Package):
        """
            Unpacks sources archive to temporary directory.
            Generates RPM SPEC file from unpacked sources.
            Builds RPM package from built SPEC file.
        """
        logger.info('Building SPEC file: %s...', package.sources)
        command = [self.active_python, os.path.join(package.sources, 'setup.py'), 'bdist_rpm']
        command += ['--spec-only', '--no-autoreq', '--python', '%{__python}']
        check_output(command, cwd=package.sources)

        spec = os.path.join(package.sources, 'dist', '%s.spec' % package.package)
        logger.info('Building SPEC file: %s built', spec)

        logger.info('Preparing SPEC file: %s...', spec)
        with codecs.open(spec, 'r', 'utf-8') as fp:
            lines = fp.readlines()
        lines = self._patch_spec_data(package, lines)

        spec = os.path.join(self.specs, '%s%s.spec' % (self.prefix, package.package))
        with codecs.open(spec, 'w', 'utf-8') as fp:
            fp.write(''.join(lines))
        logger.info('Preparing SPEC file: %s prepared', spec)


def check_output(command: typing.List[str], **kwargs):
    """ Calls command and returns output. """
    kwargs['encoding'] = 'utf-8'
    kwargs['stderr'] = subprocess.PIPE
    return subprocess.check_output(command, **kwargs)


def main():
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    handler.setLevel(logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        'package',
        help='pip package expression to packetize',
    )
    parser.add_argument(
        '--prefix',
        help='packages names prefix',
        default='',
    )
    parser.add_argument(
        '--recursive',
        help='build also all dependencies',
        action='store_true',
        default=False,
    )
    parser.add_argument(
        '--exclude',
        help='regex exclude dependencies',
        default='',
    )
    parser.add_argument(
        '--pip-index-url',
        help='pip index url',
        default='https://pypi.org/simple'
    )
    parser.add_argument(
        '--pip-json-url',
        help='pip json url',
        default='https://pypi.org/pypi/%s/json'
    )
    args = parser.parse_args()

    # Parse package expression (package name and version expression).
    search = re.search(r'^([^<>=]+)(.*)$', args.package)
    package, verexpr = search.group(1), search.group(2)

    # Start working on package.
    packetizer = Packetizer(sys.executable, args.prefix, args.pip_index_url, args.pip_json_url)
    packetizer.packetize(package, verexpr, args.recursive, args.exclude)


if __name__ == '__main__':
    main()
