# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import logging
import uuid

import ldap

# Django
from django.dispatch import receiver
from django.contrib.auth.models import User
from django.conf import settings as django_settings
from django.core.signals import setting_changed

# django-auth-ldap
from django_auth_ldap.backend import LDAPSettings as BaseLDAPSettings
from django_auth_ldap.backend import LDAPBackend as BaseLDAPBackend
from django_auth_ldap.backend import populate_user

# radiusauth
from radiusauth.backends import RADIUSBackend as BaseRADIUSBackend

# tacacs+ auth
import tacacs_plus

# social
from social.backends.saml import OID_USERID
from social.backends.saml import SAMLAuth as BaseSAMLAuth
from social.backends.saml import SAMLIdentityProvider as BaseSAMLIdentityProvider

# Ansible Tower
from awx.conf.license import feature_enabled

logger = logging.getLogger('awx.sso.backends')


class LDAPSettings(BaseLDAPSettings):

    defaults = dict(BaseLDAPSettings.defaults.items() + {
        'ORGANIZATION_MAP': {},
        'TEAM_MAP': {},
    }.items())

    def __init__(self, prefix='AUTH_LDAP_', defaults={}):
        super(LDAPSettings, self).__init__(prefix, defaults)

        # If a DB-backed setting is specified that wipes out the
        # OPT_NETWORK_TIMEOUT, fall back to a sane default
        if ldap.OPT_NETWORK_TIMEOUT not in getattr(self, 'CONNECTION_OPTIONS', {}):
            options = getattr(self, 'CONNECTION_OPTIONS', {})
            options[ldap.OPT_NETWORK_TIMEOUT] = 30
            self.CONNECTION_OPTIONS = options


class LDAPBackend(BaseLDAPBackend):
    '''
    Custom LDAP backend for AWX.
    '''

    settings_prefix = 'AUTH_LDAP_'

    def __init__(self, *args, **kwargs):
        self._dispatch_uid = uuid.uuid4()
        super(LDAPBackend, self).__init__(*args, **kwargs)
        setting_changed.connect(self._on_setting_changed, dispatch_uid=self._dispatch_uid)

    def __del__(self):
        setting_changed.disconnect(dispatch_uid=self._dispatch_uid)

    def _on_setting_changed(self, sender, **kwargs):
        # If any AUTH_LDAP_* setting changes, force settings to be reloaded for
        # this backend instance.
        if kwargs.get('setting', '').startswith(self.settings_prefix):
            self._settings = None

    def _get_settings(self):
        if self._settings is None:
            self._settings = LDAPSettings(self.settings_prefix)
        return self._settings

    def _set_settings(self, settings):
        self._settings = settings

    settings = property(_get_settings, _set_settings)

    def authenticate(self, username, password):
        if not self.settings.SERVER_URI:
            return None
        if not feature_enabled('ldap'):
            logger.error("Unable to authenticate, license does not support LDAP authentication")
            return None
        try:
            return super(LDAPBackend, self).authenticate(username, password)
        except Exception:
            logger.exception("Encountered an error authenticating to LDAP")
            return None

    def get_user(self, user_id):
        if not self.settings.SERVER_URI:
            return None
        if not feature_enabled('ldap'):
            logger.error("Unable to get_user, license does not support LDAP authentication")
            return None
        return super(LDAPBackend, self).get_user(user_id)

    # Disable any LDAP based authorization / permissions checking.

    def has_perm(self, user, perm, obj=None):
        return False

    def has_module_perms(self, user, app_label):
        return False

    def get_all_permissions(self, user, obj=None):
        return set()

    def get_group_permissions(self, user, obj=None):
        return set()


class RADIUSBackend(BaseRADIUSBackend):
    '''
    Custom Radius backend to verify license status
    '''

    def authenticate(self, username, password):
        if not django_settings.RADIUS_SERVER:
            return None
        if not feature_enabled('enterprise_auth'):
            logger.error("Unable to authenticate, license does not support RADIUS authentication")
            return None
        return super(RADIUSBackend, self).authenticate(username, password)

    def get_user(self, user_id):
        if not django_settings.RADIUS_SERVER:
            return None
        if not feature_enabled('enterprise_auth'):
            logger.error("Unable to get_user, license does not support RADIUS authentication")
            return None
        return super(RADIUSBackend, self).get_user(user_id)

    def get_django_user(self, username, password=None):
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            user = User(username=username)

        if password is not None:
            user.set_unusable_password()
            user.save()

        return user


class TACACSPlusBackend(object):
    '''
    Custom TACACS+ auth backend for AWX
    '''
    def _get_or_set_user(self, username, password):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={'is_superuser': False},
        )
        if created:
            logger.debug("Created TACACS+ user %s" % (username,))
        if password is not None:
            user.set_unusable_password()
            user.save()
        return user

    def authenticate(self, username, password):
        if not django_settings.TACACSPLUS_HOST:
            return None
        if not feature_enabled('enterprise_auth'):
            logger.error("Unable to authenticate, license does not support TACACS+ authentication")
            return None
        try:
            # Upstream TACACS+ client does not accept non-string, so convert if needed.
            auth = tacacs_plus.TACACSClient(
                django_settings.TACACSPLUS_HOST.encode('utf-8'),
                django_settings.TACACSPLUS_PORT,
                django_settings.TACACSPLUS_SECRET.encode('utf-8'),
                timeout=django_settings.TACACSPLUS_SESSION_TIMEOUT,
            ).authenticate(
                username.encode('utf-8'), password.encode('utf-8'),
                tacacs_plus.TAC_PLUS_AUTHEN_TYPES[django_settings.TACACSPLUS_AUTH_PROTOCOL],
            )
        except Exception as e:
            logger.exception("TACACS+ Authentication Error: %s" % (e.message,))
            return None
        if auth.valid:
            return self._get_or_set_user(username, password)
        else:
            return None
        return None

    def get_user(self, user_id):
        if not django_settings.TACACSPLUS_HOST:
            return None
        if not feature_enabled('enterprise_auth'):
            logger.error("Unable to get user, license does not support TACACS+ authentication")
            return None
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None


class TowerSAMLIdentityProvider(BaseSAMLIdentityProvider):
    '''
    Custom Identity Provider to make attributes to what we expect.
    '''

    def get_user_permanent_id(self, attributes):
        uid = attributes[self.conf.get('attr_user_permanent_id', OID_USERID)]
        if isinstance(uid, basestring):
            return uid
        return uid[0]

    def get_attr(self, attributes, conf_key, default_attribute):
        """
        Get the attribute 'default_attribute' out of the attributes,
        unless self.conf[conf_key] overrides the default by specifying
        another attribute to use.
        """
        key = self.conf.get(conf_key, default_attribute)
        value = attributes[key][0] if key in attributes else None
        if conf_key in ('attr_first_name', 'attr_last_name', 'attr_username', 'attr_email') and value is None:
            logger.warn("Could not map user detail '%s' from SAML attribute '%s'; "
                        "update SOCIAL_AUTH_SAML_ENABLED_IDPS['%s']['%s'] with the correct SAML attribute.",
                        conf_key[5:], key, self.name, conf_key)
        return unicode(value) if value is not None else value


class SAMLAuth(BaseSAMLAuth):
    '''
    Custom SAMLAuth backend to verify license status
    '''

    def get_idp(self, idp_name):
        idp_config = self.setting('ENABLED_IDPS')[idp_name]
        return TowerSAMLIdentityProvider(idp_name, **idp_config)

    def authenticate(self, *args, **kwargs):
        if not all([django_settings.SOCIAL_AUTH_SAML_SP_ENTITY_ID, django_settings.SOCIAL_AUTH_SAML_SP_PUBLIC_CERT,
                    django_settings.SOCIAL_AUTH_SAML_SP_PRIVATE_KEY, django_settings.SOCIAL_AUTH_SAML_ORG_INFO,
                    django_settings.SOCIAL_AUTH_SAML_TECHNICAL_CONTACT, django_settings.SOCIAL_AUTH_SAML_SUPPORT_CONTACT,
                    django_settings.SOCIAL_AUTH_SAML_ENABLED_IDPS]):
            return None
        if not feature_enabled('enterprise_auth'):
            logger.error("Unable to authenticate, license does not support SAML authentication")
            return None
        return super(SAMLAuth, self).authenticate(*args, **kwargs)

    def get_user(self, user_id):
        if not all([django_settings.SOCIAL_AUTH_SAML_SP_ENTITY_ID, django_settings.SOCIAL_AUTH_SAML_SP_PUBLIC_CERT,
                    django_settings.SOCIAL_AUTH_SAML_SP_PRIVATE_KEY, django_settings.SOCIAL_AUTH_SAML_ORG_INFO,
                    django_settings.SOCIAL_AUTH_SAML_TECHNICAL_CONTACT, django_settings.SOCIAL_AUTH_SAML_SUPPORT_CONTACT,
                    django_settings.SOCIAL_AUTH_SAML_ENABLED_IDPS]):
            return None
        if not feature_enabled('enterprise_auth'):
            logger.error("Unable to get_user, license does not support SAML authentication")
            return None
        return super(SAMLAuth, self).get_user(user_id)


def _update_m2m_from_groups(user, ldap_user, rel, opts, remove=True):
    '''
    Hepler function to update m2m relationship based on LDAP group membership.
    '''
    should_add = False
    if opts is None:
        return
    elif not opts:
        pass
    elif opts is True:
        should_add = True
    else:
        if isinstance(opts, basestring):
            opts = [opts]
        for group_dn in opts:
            if not isinstance(group_dn, basestring):
                continue
            if ldap_user._get_groups().is_member_of(group_dn):
                should_add = True
    if should_add:
        rel.add(user)
    elif remove:
        rel.remove(user)


@receiver(populate_user, dispatch_uid='populate-ldap-user')
def on_populate_user(sender, **kwargs):
    '''
    Handle signal from LDAP backend to populate the user object.  Update user
    organization/team memberships according to their LDAP groups.
    '''
    from awx.main.models import Organization, Team
    user = kwargs['user']
    ldap_user = kwargs['ldap_user']
    backend = ldap_user.backend

    # Prefetch user's groups to prevent LDAP queries for each org/team when
    # checking membership.
    ldap_user._get_groups().get_group_dns()

    # Update organization membership based on group memberships.
    org_map = getattr(backend.settings, 'ORGANIZATION_MAP', {})
    for org_name, org_opts in org_map.items():
        org, created = Organization.objects.get_or_create(name=org_name)
        remove = bool(org_opts.get('remove', True))
        admins_opts = org_opts.get('admins', None)
        remove_admins = bool(org_opts.get('remove_admins', remove))
        _update_m2m_from_groups(user, ldap_user, org.admin_role.members, admins_opts,
                                remove_admins)
        users_opts = org_opts.get('users', None)
        remove_users = bool(org_opts.get('remove_users', remove))
        _update_m2m_from_groups(user, ldap_user, org.member_role.members, users_opts,
                                remove_users)

    # Update team membership based on group memberships.
    team_map = getattr(backend.settings, 'TEAM_MAP', {})
    for team_name, team_opts in team_map.items():
        if 'organization' not in team_opts:
            continue
        org, created = Organization.objects.get_or_create(name=team_opts['organization'])
        team, created = Team.objects.get_or_create(name=team_name, organization=org)
        users_opts = team_opts.get('users', None)
        remove = bool(team_opts.get('remove', True))
        _update_m2m_from_groups(user, ldap_user, team.member_role.members, users_opts,
                                remove)

    # Update user profile to store LDAP DN.
    profile = user.profile
    if profile.ldap_dn != ldap_user.dn:
        profile.ldap_dn = ldap_user.dn
        profile.save()
