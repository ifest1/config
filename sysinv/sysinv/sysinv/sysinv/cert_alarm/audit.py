#
# Copyright (c) 2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
from datetime import datetime
from datetime import timedelta
from fm_api import constants as fm_constants
from itertools import chain
from oslo_log import log
import re
from sysinv.cert_alarm import fm as fm_mgr
from sysinv.cert_alarm import utils
from sysinv.common import constants
from sysinv.common import kubernetes as sys_kube

LOG = log.getLogger(__name__)


class CertAlarmAudit(object):
    def __init__(self):
        LOG.info('Initializing CertAlarmAudit')
        self.fm_obj = fm_mgr.FaultApiMgr()

    # ============== Full audit ===================
    def run_full_audit(self):
        """
        Run full audit
        """
        LOG.info('Running cert-alarm full audit')

        # Reset both CERT_SNAPSHOT & ALARM_SNAPSHOT
        utils.reset_cert_snapshot()
        self.fm_obj.reset_alarms_snapshot()
        self.fm_obj.reset_entityid_to_certname_map()

        # Collect CERT_SNAPSHOT
        self.collect_cert_snapshot()
        utils.print_cert_snapshot()

        self.compute_action_full_audit()

        LOG.info('cert-alarm full completed')

    def collect_cert_snapshot(self):
        """
        Collects snapshot of the certificates in the following order:
        1. K8s secret/certificates
        2. Platform certificate files
        3. SSL_CA certificate files
        """
        # 1. Process all k8s secrets/certificates
        LOG.debug('Processing k8s secret/certificates...')
        try:
            all_secrets = utils.get_tls_secrets_from_all_ns()

            if all_secrets:
                LOG.info('Number of TLS secrets to process=%d' % len(all_secrets))
                for item in all_secrets:
                    LOG.info('Processing item: %s' % item.metadata.name)
                    (certname_secret, exp_date_secret, anno_data_secret, mode_metadata) = \
                            utils.collect_certificate_data_from_kube_secret(item)
                    # if cert not present, exp_date will be None
                    if exp_date_secret is not None:
                        utils.add_cert_snapshot(certname_secret,
                                                exp_date_secret,
                                                anno_data_secret,
                                                mode_metadata)
        except Exception as e:
            LOG.error(e)

        # 2. Process platform certs stored as pem files
        LOG.debug('Processing platform certificate files...')
        for key, value in constants.CERT_LOCATION_MAP.items():
            LOG.info('Processing item: %s at location %s' % (key, value))
            if utils.is_certname_already_processed(key) is True:
                continue

            (certname_file, exp_date_file, anno_data_file, mode_metadata_file) = \
                    utils.collect_certificate_data_from_file(key, value)
            # if cert not present, exp_date will be None
            if exp_date_file is not None:
                utils.add_cert_snapshot(certname_file,
                                        exp_date_file,
                                        anno_data_file,
                                        mode_metadata_file)

        # 3. Process SSL_CA certificates (special case, since there can be multiple files)
        LOG.debug('Processing ssl_ca certificate files...')
        ssl_ca_data_list = utils.collect_certificate_data_for_ssl_cas()
        LOG.debug('ssl_ca_data_list=%s' % ssl_ca_data_list)
        for entry in ssl_ca_data_list:
            # if cert not present, exp_date will be None
            if entry[1] is not None:
                utils.add_cert_snapshot(entry[0], entry[1], entry[2], entry[3])

    def compute_action_full_audit(self):
        for cert_name in utils.CERT_SNAPSHOT:
            self.compute_action(cert_name)

    # ============== Active Alarm audit ===================
    def run_active_alarm_audit(self):
        """
        Run audit only on active alarms
        """
        LOG.info('Running cert-alarm active_alarm_audit')

        # Collect ALARM_SNAPSHOT
        self.fm_obj.collect_all_cert_alarms()

        self.compute_action_active_alarms()

        LOG.info('cert-alarm active_alarm_audit completed')

    def compute_action_active_alarms(self):
        # Create single list of expiring_soon & expired certs
        all_active_alarms = list(chain(*self.fm_obj.ALARMS_SNAPSHOT.values()))

        for cert_name in all_active_alarms:
            # 1. First refresh expiry date snapshot data
            self.refresh_expiry_data(cert_name)

            # 2. Now check dates and compute_action
            self.compute_action(cert_name)

    def refresh_expiry_data(self, cert_name):
        if cert_name not in utils.CERT_SNAPSHOT:
            LOG.error('Could not find cert %s in snapshot to refresh expiry data' % cert_name)
        else:
            snapshot = utils.CERT_SNAPSHOT[cert_name]
            if snapshot[utils.SNAPSHOT_KEY_MODE] is utils.MODE_SECRET or \
                        snapshot[utils.SNAPSHOT_KEY_MODE] is utils.MODE_CERT_MGR:
                # mode is k8s secret
                kube_op = sys_kube.KubeOperator()
                try:
                    secobj = kube_op.kube_get_secret(snapshot[utils.SNAPSHOT_KEY_k8s_secret],
                                                     snapshot[utils.SNAPSHOT_KEY_k8s_ns])
                    (certname_secret, exp_date_secret, anno_data_secret, mode_metadata) = \
                            utils.collect_certificate_data_from_kube_secret(secobj)
                    if exp_date_secret is not None:
                        utils.add_cert_snapshot(certname_secret,
                                                exp_date_secret,
                                                anno_data_secret,
                                                mode_metadata)
                except Exception as e:
                    LOG.error("Failed to retrieve k8s_secret %s" % e)
            else:
                # mode is file
                (certname_file, exp_date_file, anno_data_file, mode_metadata_file) = \
                    utils.collect_certificate_data_from_file(cert_name,
                                                             snapshot[utils.SNAPSHOT_KEY_FILE_LOC])
                if exp_date_file is not None:
                    utils.add_cert_snapshot(certname_file,
                                            exp_date_file,
                                            anno_data_file,
                                            mode_metadata_file)

    # ============== Common ===================

    @staticmethod
    def parse_time(time_str):
        regex = re.compile(r'((?P<weeks>\d+?)w)?((?P<days>\d+?)d)?((?P<hours>\d+?)h)?')
        parts = regex.match(time_str).groupdict()
        time_params = {}
        for name, param in parts.items():
            if param:
                time_params[name] = int(param)
        return timedelta(**time_params)

    def compute_action(self, cert_name):
        """
        Computes any action required based on parameters passed and calls FM API
        Input:  cert_name: Certificate name
        """
        if cert_name not in utils.CERT_SNAPSHOT:
            LOG.error('Could not find cert %s in snapshot' % cert_name)
            return

        snapshot = utils.CERT_SNAPSHOT[cert_name]
        expiry = snapshot[utils.SNAPSHOT_KEY_EXPDATE] - datetime.now()
        alarm_before = self.parse_time(snapshot.get(constants.CERT_ALARM_ANNOTATION_ALARM_BEFORE,
                                       constants.CERT_ALARM_DEFAULT_ANNOTATION_ALARM_BEFORE))
        renew_before = None
        if utils.SNAPSHOT_KEY_RENEW_BEFORE in snapshot:
            renew_before = self.parse_time(snapshot[utils.SNAPSHOT_KEY_RENEW_BEFORE])
        LOG.debug('cert_name=%s, expiry=%s, alarm_before=%s, renew_before=%s'
            % (cert_name, expiry.days, alarm_before.days, renew_before.days))

        days_to_expiry = expiry.days
        alarm_before_days = alarm_before.days
        renew_before_days = renew_before.days

        # set threshold date to raise alarms
        if renew_before_days:
            # if renew_before_days valid, take latest (smaller timedelta) of two dates as threshold
            threshold = renew_before_days if renew_before_days < alarm_before_days else alarm_before_days
        else:
            threshold = alarm_before_days

        if days_to_expiry > threshold:
            self.clear_expiring_soon(cert_name)
            self.clear_expired(cert_name)
        else:
            if days_to_expiry > 0:
                self.raise_expiring_soon(cert_name)
            else:
                # Expired. Clear expiring-soon & raise expired
                self.clear_expiring_soon(cert_name)
                self.raise_expired(cert_name)

    def raise_expiring_soon(self, cert_name):
        self.fm_obj.set_fault(cert_name,
                              False,
                              fm_constants.FM_ALARM_STATE_SET)

    def clear_expiring_soon(self, cert_name):
        self.fm_obj.set_fault(cert_name,
                              False,
                              fm_constants.FM_ALARM_STATE_CLEAR)

    def raise_expired(self, cert_name):
        self.fm_obj.set_fault(cert_name,
                              True,
                              fm_constants.FM_ALARM_STATE_SET)

    def clear_expired(self, cert_name):
        self.fm_obj.set_fault(cert_name,
                              True,
                              fm_constants.FM_ALARM_STATE_CLEAR)