"""Prepares a distribution for installation
"""

import logging
import os

from pip._vendor import pkg_resources, requests

from pip.compat import expanduser
from pip.download import (
    is_dir_url, is_file_url, is_vcs_url, unpack_url, url_to_path
)
from pip.exceptions import (
    DirectoryUrlHashUnsupported, HashUnpinned, InstallationError,
    PreviousBuildDirError, VcsHashUnsupported
)
from pip.utils import display_path, dist_in_usersite, normalize_path
from pip.utils.hashes import MissingHashes
from pip.utils.logging import indent_log
from pip.vcs import vcs

logger = logging.getLogger(__name__)


def make_abstract_dist(req):
    """Factory to make an abstract dist object.

    Preconditions: Either an editable req with a source_dir, or satisfied_by or
    a wheel link, or a non-editable req with a source_dir.

    :return: A concrete DistAbstraction.
    """
    if req.editable:
        return IsSDist(req)
    elif req.link and req.link.is_wheel:
        return IsWheel(req)
    else:
        return IsSDist(req)


class DistAbstraction(object):
    """Abstracts out the wheel vs non-wheel Resolver.resolve() logic.

    The requirements for anything installable are as follows:
     - we must be able to determine the requirement name
       (or we can't correctly handle the non-upgrade case).
     - we must be able to generate a list of run-time dependencies
       without installing any additional packages (or we would
       have to either burn time by doing temporary isolated installs
       or alternatively violate pips 'don't start installing unless
       all requirements are available' rule - neither of which are
       desirable).
     - for packages with setup requirements, we must also be able
       to determine their requirements without installing additional
       packages (for the same reason as run-time dependencies)
     - we must be able to create a Distribution object exposing the
       above metadata.
    """

    def __init__(self, req):
        self.req = req

    def dist(self, finder):
        """Return a setuptools Dist object."""
        raise NotImplementedError(self.dist)

    def prep_for_dist(self):
        """Ensure that we can get a Dist for this requirement."""
        raise NotImplementedError(self.dist)


class IsWheel(DistAbstraction):

    def dist(self, finder):
        return list(pkg_resources.find_distributions(
            self.req.source_dir))[0]

    def prep_for_dist(self):
        # FIXME:https://github.com/pypa/pip/issues/1112
        pass


class IsSDist(DistAbstraction):

    def dist(self, finder):
        dist = self.req.get_dist()
        # FIXME: shouldn't be globally added.
        if dist.has_metadata('dependency_links.txt'):
            finder.add_dependency_links(
                dist.get_metadata_lines('dependency_links.txt')
            )
        return dist

    def prep_for_dist(self):
        self.req.run_egg_info()
        self.req.assert_source_matches_version()


class Installed(DistAbstraction):

    def dist(self, finder):
        return self.req.satisfied_by

    def prep_for_dist(self):
        pass


class RequirementPreparer(object):
    """Prepares a Requirement
    """

    def __init__(self, build_dir, download_dir, src_dir, wheel_download_dir,
                 progress_bar):
        super(RequirementPreparer, self).__init__()

        self.src_dir = src_dir
        self.build_dir = build_dir

        # Where still packed archives should be written to. If None, they are
        # not saved, and are deleted immediately after unpacking.
        self.download_dir = download_dir

        # Where still-packed .whl files should be written to. If None, they are
        # written to the download_dir parameter. Separate to download_dir to
        # permit only keeping wheel archives for pip wheel.
        if wheel_download_dir:
            wheel_download_dir = normalize_path(wheel_download_dir)
        self.wheel_download_dir = wheel_download_dir

        # NOTE
        # download_dir and wheel_download_dir overlap semantically and may
        # be combined if we're willing to have non-wheel archives present in
        # the wheelhouse output by 'pip wheel'.

        self.progress_bar = progress_bar

    @property
    def _download_should_save(self):
        # TODO: Modify to reduce indentation needed
        if self.download_dir:
            self.download_dir = expanduser(self.download_dir)
            if os.path.exists(self.download_dir):
                return True
            else:
                logger.critical('Could not find download directory')
                raise InstallationError(
                    "Could not find or access download directory '%s'"
                    % display_path(self.download_dir))
        return False

    def prepare_requirement(self, req, resolver):
        # TODO: Breakup into smaller functions
        # TODO: Add a nice docstring
        if req.editable:
            logger.info('Obtaining %s', req)
        else:
            # satisfied_by is only evaluated by calling _check_skip_installed,
            # so it must be None here.
            assert req.satisfied_by is None
            if not resolver.ignore_installed:
                skip_reason = resolver._check_skip_installed(req)

            if req.satisfied_by:
                assert skip_reason is not None, (
                    '_check_skip_installed returned None but '
                    'req.satisfied_by is set to %r'
                    % (req.satisfied_by,))
                logger.info(
                    'Requirement %s: %s (%s)', skip_reason,
                    req,
                    req.satisfied_by.version)
            else:
                if (req.link and
                        req.link.scheme == 'file'):
                    path = url_to_path(req.link.url)
                    logger.info('Processing %s', display_path(path))
                else:
                    logger.info('Collecting %s', req)

        assert resolver.require_hashes is not None, \
            "This should have been set in resolve()"

        with indent_log():
            # ################################ #
            # # vcs update or unpack archive # #
            # ################################ #
            if req.editable:
                if resolver.require_hashes:
                    raise InstallationError(
                        'The editable requirement %s cannot be installed when '
                        'requiring hashes, because there is no single file to '
                        'hash.' % req)
                req.ensure_has_source_dir(self.src_dir)
                req.update_editable(not self._download_should_save)
                abstract_dist = make_abstract_dist(req)
                abstract_dist.prep_for_dist()
                if self._download_should_save:
                    req.archive(self.download_dir)
                req.check_if_exists()
            elif req.satisfied_by:
                if resolver.require_hashes:
                    logger.debug(
                        'Since it is already installed, we are trusting this '
                        'package without checking its hash. To ensure a '
                        'completely repeatable environment, install into an '
                        'empty virtualenv.')
                abstract_dist = Installed(req)
            else:
                # @@ if filesystem packages are not marked
                # editable in a req, a non deterministic error
                # occurs when the script attempts to unpack the
                # build directory
                req.ensure_has_source_dir(self.build_dir)
                # If a checkout exists, it's unwise to keep going.  version
                # inconsistencies are logged later, but do not fail the
                # installation.
                # FIXME: this won't upgrade when there's an existing
                # package unpacked in `req.source_dir`
                # package unpacked in `req.source_dir`
                if os.path.exists(
                        os.path.join(req.source_dir, 'setup.py')):
                    raise PreviousBuildDirError(
                        "pip can't proceed with requirements '%s' due to a"
                        " pre-existing build directory (%s). This is "
                        "likely due to a previous installation that failed"
                        ". pip is being responsible and not assuming it "
                        "can delete this. Please delete it and try again."
                        % (req, req.source_dir)
                    )
                req.populate_link(
                    resolver.finder,
                    resolver._is_upgrade_allowed(req),
                    resolver.require_hashes
                )
                # We can't hit this spot and have populate_link return None.
                # req.satisfied_by is None here (because we're
                # guarded) and upgrade has no impact except when satisfied_by
                # is not None.
                # Then inside find_requirement existing_applicable -> False
                # If no new versions are found, DistributionNotFound is raised,
                # otherwise a result is guaranteed.
                assert req.link
                link = req.link

                # Now that we have the real link, we can tell what kind of
                # requirements we have and raise some more informative errors
                # than otherwise. (For example, we can raise VcsHashUnsupported
                # for a VCS URL rather than HashMissing.)
                if resolver.require_hashes:
                    # We could check these first 2 conditions inside
                    # unpack_url and save repetition of conditions, but then
                    # we would report less-useful error messages for
                    # unhashable requirements, complaining that there's no
                    # hash provided.
                    if is_vcs_url(link):
                        raise VcsHashUnsupported()
                    elif is_file_url(link) and is_dir_url(link):
                        raise DirectoryUrlHashUnsupported()
                    if (not req.original_link and
                            not req.is_pinned):
                        # Unpinned packages are asking for trouble when a new
                        # version is uploaded. This isn't a security check, but
                        # it saves users a surprising hash mismatch in the
                        # future.
                        #
                        # file:/// URLs aren't pinnable, so don't complain
                        # about them not being pinned.
                        raise HashUnpinned()
                hashes = req.hashes(
                    trust_internet=not resolver.require_hashes)
                if resolver.require_hashes and not hashes:
                    # Known-good hashes are missing for this requirement, so
                    # shim it with a facade object that will provoke hash
                    # computation and then raise a HashMissing exception
                    # showing the user what the hash should be.
                    hashes = MissingHashes()

                try:
                    download_dir = self.download_dir
                    # We always delete unpacked sdists after pip ran.
                    autodelete_unpacked = True
                    if req.link.is_wheel \
                            and self.wheel_download_dir:
                        # when doing 'pip wheel` we download wheels to a
                        # dedicated dir.
                        download_dir = self.wheel_download_dir
                    if req.link.is_wheel:
                        if download_dir:
                            # When downloading, we only unpack wheels to get
                            # metadata.
                            autodelete_unpacked = True
                        else:
                            # When installing a wheel, we use the unpacked
                            # wheel.
                            autodelete_unpacked = False
                    unpack_url(
                        req.link, req.source_dir,
                        download_dir, autodelete_unpacked,
                        session=resolver.session, hashes=hashes,
                        progress_bar=self.progress_bar)
                except requests.HTTPError as exc:
                    logger.critical(
                        'Could not install requirement %s because '
                        'of error %s',
                        req,
                        exc,
                    )
                    raise InstallationError(
                        'Could not install requirement %s because '
                        'of HTTP error %s for URL %s' %
                        (req, exc, req.link)
                    )
                abstract_dist = make_abstract_dist(req)
                abstract_dist.prep_for_dist()
                if self._download_should_save:
                    # Make a .zip of the source_dir we already created.
                    if req.link.scheme in vcs.all_schemes:
                        req.archive(self.download_dir)
                # req.req is only avail after unpack for URL
                # pkgs repeat check_if_exists to uninstall-on-upgrade
                # (#14)
                if not resolver.ignore_installed:
                    req.check_if_exists()
                if req.satisfied_by:
                    should_modify = (
                        resolver.upgrade_strategy != "to-satisfy-only" or
                        resolver.ignore_installed
                    )
                    if should_modify:
                        # don't uninstall conflict if user install and
                        # conflict is not user install
                        if not (resolver.use_user_site and not
                                dist_in_usersite(req.satisfied_by)):
                            req.conflicts_with = \
                                req.satisfied_by
                        req.satisfied_by = None
                    else:
                        logger.info(
                            'Requirement already satisfied (use '
                            '--upgrade to upgrade): %s',
                            req,
                        )
        return abstract_dist
