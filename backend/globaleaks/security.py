# -*- coding: utf-8 -*-
#
# security
# ********
#
# GlobaLeaks security functions

import binascii
import os
import re
import base64
import json
import random
import shutil
import scrypt
import string
import time

from twisted.internet.defer import inlineCallbacks
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from datetime import datetime
from gnupg import GPG
from tempfile import _TemporaryFileWrapper

from globaleaks.rest import errors
from globaleaks.utils.utility import log, datetime_to_day_str, datetime_now
from globaleaks.settings import GLSettings

crypto_backend = default_backend()

def sha256(data):
    h = hashes.Hash(hashes.SHA256(), backend=crypto_backend)
    h.update(data)
    return binascii.b2a_hex(h.finalize())


def sha512(data):
    h = hashes.Hash(hashes.SHA512(), backend=crypto_backend)
    h.update(data)
    return binascii.b2a_hex(h.finalize())


def generateRandomKey(N):
    """
    Return a random key of N characters in a-zA-Z0-9
    """
    return ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(N)).encode('utf-8')


def generateRandomSalt():
    """
    Return a base64 encoded string with 128 bit of entropy
    """
    return base64.b64encode(os.urandom(16))


def generateRandomPassword():
    """
    Return a random password of 10 characters in a-zA-Z0-9
    """
    return generateRandomKey(10)


def _overwrite(absolutefpath, pattern):
    filesize = os.path.getsize(absolutefpath)
    bytecnt = 0
    with open(absolutefpath, 'w+') as f:
        f.seek(0)
        while bytecnt < filesize:
            f.write(pattern)
            bytecnt += len(pattern)


def overwrite_and_remove(absolutefpath, iterations_number=1):
    """
    Overwrite the file with all_zeros, all_ones, random patterns

    Note: At each iteration the original size of the file is altered.
    """
    if random.randint(1, 5) == 3:
        # "never let attackers do assumptions"
        iterations_number += 1

    log.debug("Starting secure deletion of file %s" % absolutefpath)

    try:
        # in the following loop, the file is open and closed on purpose, to trigger flush operations
        all_zeros = "\0\0\0\0" * 1024               # 4kb of zeros
        all_ones = "FFFFFFFF".decode("hex") * 1024  # 4kb of ones

        for iteration in xrange(iterations_number):
            OPTIMIZATION_RANDOM_BLOCK = 4096 + random.randint(1, 4096)

            random_pattern = ""
            for i in xrange(OPTIMIZATION_RANDOM_BLOCK):
                random_pattern += str(random.randrange(256))

            log.debug("Excecuting rewrite iteration (%d out of %d)" %
                      (iteration, iterations_number))

            _overwrite(absolutefpath, all_zeros)
            log.debug("Overwritten file %s with all zeros pattern" % absolutefpath)

            _overwrite(absolutefpath, all_ones)
            log.debug("Overwritten file %s with all ones pattern" % absolutefpath)

            _overwrite(absolutefpath, random_pattern)
            log.debug("Overwritten file %s with random pattern" % absolutefpath)

    except Exception as e:
        log.err("Unable to perform secure overwrite for file %s: %s" %
                (absolutefpath, e))

    finally:
        try:
            os.remove(absolutefpath)
        except OSError as remove_ose:
            log.err("Unable to perform unlink operation on file %s: %s" %
                    (absolutefpath, remove_ose))

    log.debug("Performed deletion of file file: %s" % absolutefpath)


class GLSecureTemporaryFile(_TemporaryFileWrapper):
    """
    WARNING!
    You can't use this File object like a normal file object,
    check .read and .write!
    """
    last_action = 'init'

    def __init__(self, filedir):
        """
        filedir: directory where to store files
        """
        self.creation_date = time.time()

        self.create_key()
        self.encryptor_finalized = False

        # XXX remind enhance file name with incremental number
        self.filepath = os.path.join(filedir, "%s.aes" % self.key_id)

        log.debug("++ Creating %s filetmp" % self.filepath)

        self.file = open(self.filepath, 'w+b')

        # last argument is 'True' because the file has to be deleted on .close()
        _TemporaryFileWrapper.__init__(self, self.file, self.filepath, True)

    def initialize_cipher(self):
        self.cipher = Cipher(algorithms.AES(self.key), modes.CTR(self.key_counter_nonce), backend=crypto_backend)
        self.encryptor = self.cipher.encryptor()
        self.decryptor = self.cipher.decryptor()

    def create_key(self):
        """
        Create the AES Key to encrypt uploaded file.
        """
        self.key = os.urandom(GLSettings.AES_key_size)

        self.key_id = generateRandomKey(16)
        self.keypath = os.path.join(GLSettings.ramdisk_path, "%s%s" %
                                    (GLSettings.AES_keyfile_prefix, self.key_id))

        while os.path.isfile(self.keypath):
            self.key_id = generateRandomKey(16)
            self.keypath = os.path.join(GLSettings.ramdisk_path, "%s%s" %
                                        (GLSettings.AES_keyfile_prefix, self.key_id))

        self.key_counter_nonce = os.urandom(GLSettings.AES_counter_nonce)
        self.initialize_cipher()

        key_json = {
            'key': base64.b64encode(self.key),
            'key_counter_nonce': base64.b64encode(self.key_counter_nonce)
        }

        log.debug("Key initialization at %s" % self.keypath)

        with open(self.keypath, 'w') as kf:
            json.dump(key_json, kf)

        if not os.path.isfile(self.keypath):
            log.err("Unable to write keyfile %s" % self.keypath)
            raise Exception("Unable to write keyfile %s" % self.keypath)

    def avoid_delete(self):
        log.debug("Avoid delete on: %s " % self.filepath)
        self.delete = False

    def write(self, data):
        """
        The last action is kept track because the internal status
        need to track them. read below read()
        """
        assert (self.last_action != 'read'), "you can write after read!"

        self.last_action = 'write'
        try:
            if isinstance(data, unicode):
                data = data.encode('utf-8')

            self.file.write(self.encryptor.update(data))
        except Exception as wer:
            log.err("Unable to write() in GLSecureTemporaryFile: %s" % wer.message)
            raise wer

    def close(self):
        if not self.close_called:
            try:
                if any(x in self.file.mode for x in 'wa') and not self.encryptor_finalized:
                    self.encryptor_finalized = True
                    self.file.write(self.encryptor.finalize())

            except:
                pass

            finally:
                if self.delete:
                    os.remove(self.keypath)

        try:
            _TemporaryFileWrapper.close(self)
        except:
            pass

    def read(self, c=None):
        """
        The first time 'read' is called after a write, seek(0) is performed
        """
        if self.last_action == 'write':
            if any(x in self.file.mode for x in 'wa') and not self.encryptor_finalized:
                self.encryptor_finalized = True
                self.file.write(self.encryptor.finalize())

            self.seek(0, 0)  # this is a trick just to misc write and read
            self.initialize_cipher()
            log.debug("First seek on %s" % self.filepath)
            self.last_action = 'read'

        data = None
        if c is None:
            data = self.file.read()
        else:
            data = self.file.read(c)

        if len(data):
            return self.decryptor.update(data)
        else:
            return self.decryptor.finalize()


class GLSecureFile(GLSecureTemporaryFile):
    def __init__(self, filepath):
        self.filepath = filepath

        self.key_id = os.path.basename(self.filepath).split('.')[0]

        log.debug("Opening secure file %s with %s" % (self.filepath, self.key_id))

        self.file = open(self.filepath, 'r+b')

        # last argument is 'False' because the file has not to be deleted on .close()
        _TemporaryFileWrapper.__init__(self, self.file, self.filepath, False)

        self.load_key()

    def load_key(self):
        """
        Load the AES Key to decrypt uploaded file.
        """
        self.keypath = os.path.join(GLSettings.ramdisk_path, ("%s%s" % (GLSettings.AES_keyfile_prefix, self.key_id)))

        try:
            with open(self.keypath, 'r') as kf:
                key_json = json.load(kf)

            self.key = base64.b64decode(key_json['key'])
            self.key_counter_nonce = base64.b64decode(key_json['key_counter_nonce'])
            self.initialize_cipher()

        except Exception as axa:
            # I'm sorry, those file is a dead file!
            log.err("The file %s has been encrypted with a lost/invalid key (%s)" % (self.keypath, axa.message))
            raise axa


def directory_traversal_check(trusted_absolute_prefix, untrusted_path):
    """
    check that an 'untrusted_path' match a 'trusted_absolute_path' prefix
    """
    if not os.path.isabs(trusted_absolute_prefix):
        raise Exception("programming error: trusted_absolute_prefix is not an absolute path: %s" %
                        trusted_absolute_prefix)

    untrusted_path = os.path.abspath(untrusted_path)

    if trusted_absolute_prefix != os.path.commonprefix([trusted_absolute_prefix, untrusted_path]):
        log.err("Blocked file operation out of the expected path: (\"%s\], \"%s\"" %
                (trusted_absolute_prefix, untrusted_path))

        raise errors.DirectoryTraversalError


def derive_auth_hash(password, salt):
    pw, s = password.encode('utf8'), salt.encode('utf8')
    digest = scrypt_password(pw, s) 
    return sha512(digest)


def scrypt_password(password, salt):
    return scrypt.hash(password, salt, buflen=256)

class GLBPGP(object):
    """
    PGP has not a dedicated class, because one of the function is called inside a transact, and
    I'm not quite confident on creating an object that operates on the filesystem knowing
    that would be run also on the Storm cycle.
    """
    def __init__(self):
        """
        every time is needed, a new keyring is created here.
        """
        try:
            temp_pgproot = os.path.join(GLSettings.pgproot, "%s" % generateRandomKey(8))
            os.makedirs(temp_pgproot, mode=0700)
            self.gnupg = GPG(gnupghome=temp_pgproot, options=['--trust-model', 'always'])
            self.gnupg.encoding = "UTF-8"
        except OSError as ose:
            log.err("Critical, OS error in operating with GnuPG home: %s" % ose)
            raise
        except Exception as excep:
            log.err("Unable to instance PGP object: %s" % excep)
            raise

    def load_key(self, key):
        """
        @param key:
        @return: True or False, True only if a key is effectively importable and listed.
        """
        try:
            import_result = self.gnupg.import_keys(key)
        except Exception as excep:
            log.err("Error in PGP import_keys: %s" % excep)
            raise errors.PGPKeyInvalid

        if len(import_result.fingerprints) == 0:
            raise errors.PGPKeyInvalid

        fingerprint = import_result.fingerprints[0]

        # looking if the key is effectively reachable
        try:
            all_keys = self.gnupg.list_keys()
        except Exception as excep:
            log.err("Error in PGP list_keys: %s" % excep)
            raise errors.PGPKeyInvalid

        expiration = datetime.utcfromtimestamp(0)
        for key in all_keys:
            if key['fingerprint'] == fingerprint:
                if key['expires']:
                    expiration = datetime.utcfromtimestamp(int(key['expires']))
                break

        return {
            'fingerprint': fingerprint,
            'expiration': expiration,
        }

    def encrypt_file(self, key_fingerprint, input_file, output_path):
        """
        Encrypt a file with the specified PGP key
        """
        encrypted_obj = self.gnupg.encrypt_file(input_file, str(key_fingerprint), output=output_path)

        if not encrypted_obj.ok:
            return None, 0

        return encrypted_obj,  os.stat(output_path).st_size

    def encrypt_message(self, key_fingerprint, plaintext):
        """
        Encrypt a text message with the specified key
        """
        encrypted_obj = self.gnupg.encrypt(plaintext, str(key_fingerprint))

        if not encrypted_obj.ok:
            return None

        return str(encrypted_obj)

    def destroy_environment(self):
        try:
            shutil.rmtree(self.gnupg.gnupghome)
        except Exception as excep:
            log.err("Unable to clean temporary PGP environment: %s: %s" % (self.gnupg.gnupghome, excep))
