# ThicketDB: Laubwerk Plants database for Thicket Blender Add-on
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# Copyright (C) 2020 Darren Hart <dvhart@infradead.org>

import argparse
from collections import deque
import glob
import hashlib
import json
import logging
import os
from pathlib import Path
from subprocess import Popen, PIPE
import sys
import textwrap
try:
    import laubwerk as lbw
    from . import logger
except ImportError:
    # Likely running as a subprocess, these will be added in main()
    pass

# <pep8 compliant>

SCHEMA_VERSION = 2


def md5sum(filename):
    md5 = hashlib.md5()
    with open(filename, mode="rb") as f:
        buf = f.read(4096)
        while buf:
            md5.update(buf)
            buf = f.read(4096)
    return md5.hexdigest()


class DBSeason:
    def __init__(self, db, name):
        self.name = name
        self.label = db.get_label(name)


class DBVariant:
    def __init__(self, db, name, v_rec, model_preview):
        self.name = name
        self.label = db.get_label(self.name)
        self.seasons = [DBSeason(db, s) for s in v_rec["seasons"]]
        self._default_season = DBSeason(db, v_rec["default_season"])
        self.preview = v_rec["preview"]
        if self.preview == "":
            self.preview = model_preview

    def get_season(self, name=None):
        """ Return the requested season or the default season if None or not found """
        if name is not None:
            for s in self.seasons:
                if s.name == name:
                    return s
        return self._default_season


class DBModel:
    def __init__(self, db, name):
        self.name = name
        m_rec = db._db["models"][name]
        self.md5 = m_rec["md5"]
        self.filepath = m_rec["filepath"]
        self.label = db.get_label(self.name)
        preview = m_rec["preview"]
        self.variants = [DBVariant(db, v, m_rec["variants"][v], preview) for v in m_rec["variants"]]
        def_v = m_rec["default_variant"]
        self._default_variant = DBVariant(db, def_v, m_rec["variants"][def_v], preview)
        self.preview = preview

    def get_variant(self, name=None):
        """ Return the requested variant or the default variant if None or not found """
        if name is not None:
            for v in self.variants:
                if v.name == name:
                    return v
        return self._default_variant


class DBIter:
    def __init__(self, db):
        self._items = []
        self._index = 0

        for name in db._db["models"]:
            self._items.append(DBModel(db, name))
        self._items.sort(key=lambda model: model.name)

    def __next__(self):
        if self._index < len(self._items):
            item = self._items[self._index]
            self._index += 1
            return item
        raise StopIteration


class ThicketDBOldSchemaError(Exception):
    # TODO: include current and read schema version
    pass


class ThicketDB:
    """ Thicket Database Interface """
    def __init__(self, db_filename, locales, python=sys.executable, create=False):
        global SCHEMA_VERSION
        self._db_filename = db_filename
        self.python = python
        self.locales = locales
        try:
            with open(db_filename, "r", encoding="utf-8") as f:
                self._db = json.load(f)
            if self._db["info"]["schema_version"] < SCHEMA_VERSION:
                logger.warning("Unknown database schema version")
                raise ThicketDBOldSchemaError
        except FileNotFoundError:
            if create:
                self.initialize()
                self.save()
            else:
                raise FileNotFoundError
        except json.decoder.JSONDecodeError as e:
            logger.critical("JSONDecodeError while loading database: %s" % e)

    def __iter__(self):
        return DBIter(self)

    def initialize(self):
        global SCHEMA_VERSION
        self._db = {}
        self._db["info"] = {}
        self._db["labels"] = {}
        self._db["models"] = {}

        self._db["info"]["sdk_version"] = lbw.version
        self._db["info"]["sdk_major"] = lbw.version_info.major
        self._db["info"]["sdk_minor"] = lbw.version_info.minor
        self._db["info"]["sdk_micro"] = lbw.version_info.micro
        self._db["info"]["schema_version"] = SCHEMA_VERSION

    def save(self):
        with open(self._db_filename, "w", encoding="utf-8") as f:
            json.dump(self._db, f, ensure_ascii=False, indent=4)

    def print_info(self):
        info = self._db["info"]
        print("Laubwerk Version: %s" % info["sdk_version"])
        print("\tmajor: %s" % info["sdk_major"])
        print("\tminor: %s" % info["sdk_minor"])
        print("\tmicro: %s" % info["sdk_micro"])
        print("Loaded %d models:" % self.model_count())

    def update_labels(self, labels):
        self._db["labels"].update(labels)

    def get_translation_dict(self):
        translations_dict = {}
        for key, value in self._db["labels"].items():
            for lang, trans in value.items():
                if trans:
                    translations_dict.setdefault(lang, {})[('*', self.get_label(key))] = trans

        return translations_dict

    def get_label(self, key, defaultLocale='en_US'):
        try:
            if defaultLocale in self._db["labels"][key]:
                return self._db["labels"][key][defaultLocale]

            return key
        except KeyError:
            return key

    def get_model(self, filepath=None, name=None):
        if name:
            if name not in self._db["models"]:
                name = None

        if name is None and filepath:
            for n in self._db["models"]:
                if self._db["models"][n]["filepath"] == filepath:
                    name = n

        if name:
            return DBModel(self, name)

        return None

    def add_model(self, filepath):
        m_rec = ThicketDB.parse_model(filepath, self.locales)
        self._db["models"][m_rec["name"]] = m_rec["model"]
        self.update_labels(m_rec["labels"])

    def model_count(self):
        return len(self._db["models"])

    def build(self, models_dir, sdk_path):
        self.initialize()

        # FIXME: .gz is optional
        model_files = glob.glob(models_dir + "/*/*.lbw.gz")
        num_models = len(model_files)

        num_jobs = os.cpu_count()
        if not num_jobs:
            num_jobs = 4
        jobs = deque()

        log_level = logging.getLevelName(logger.level)
        logger.info("Parsing %d models using %d parallel jobs" % (num_models, num_jobs))
        while len(model_files) > 0 or len(jobs) > 0:
            # Keep up to num_jobs jobs running
            while len(jobs) < num_jobs and len(model_files) > 0:
                f = model_files.pop()
                logger.debug("Parsing: %s" % f)
                job = Popen([self.python, __file__, "-f", f, "-s", sdk_path, "-l", log_level, "-t", ','.join(self.locales), "parse_model"],
                            stdout=PIPE)
                jobs.append(job)

            # Wait for the oldest job to complete
            job = jobs.popleft()
            outs, errs = job.communicate()
            try:
                m_rec = json.loads(outs)
                self._db["models"][m_rec["model"]["name"]] = m_rec["model"]
                self.update_labels(m_rec["labels"])
                logger.info('Added "%s"' % m_rec["model"]["name"])
            except json.decoder.JSONDecodeError as e:
                logger.error("JSONDecodeError while parsing %s: %s" % (f, e))

        if len(model_files) > 0:
            logger.error("Exited worker loop with %d model files remaining" % len(model_files))

        if len(jobs) > 0:
            logger.error("Exited worker loop with %d jobs still running" % len(jobs))

        self.save()
        logger.info("Processed %d/%d models" % (self.model_count(), num_models))

    def read(self):
        self.print_info()

        for model in self:
            print("%s (%s)" % (model.name, model.label))
            print("\tfile: %s" % model.filepath)
            print("\tmd5: %s" % model.md5)
            v = model.get_variant()
            print("\tdefault_variant: %s (%s)" % (v.name, v.label))
            print("\tvariants:")
            for v in model.variants:
                print("\t\t%s (%s) %s" % (v.name, v.get_season().label,
                                          [s.name for s in v.seasons]))

    # Class methods
    def parse_model(filepath, locales):
        m = lbw.load(filepath)
        m_rec = {}

        model = {}
        model["name"] = m.name
        model["filepath"] = filepath
        model["md5"] = md5sum(filepath)
        params_variant = next(x for x in m.params if x['name'] == "variant")['enum']
        default_variant_idx = params_variant["default"]
        model["default_variant"] = params_variant["options"][default_variant_idx]["name"]
        preview_stem = Path(filepath).stem
        preview_path = Path(filepath).parent.absolute() / (preview_stem + ".png")
        if not preview_path.is_file():
            preview_stem = os.path.splitext(preview_stem)[0]
            preview_path = Path(filepath).parent.absolute() / (preview_stem + ".png")
            if not preview_path.is_file():
                logger.warning("Preview not found: %s" % preview_path)
                preview_path = ""
        model["preview"] = str(preview_path)

        labels = {}
        m_labels = ThicketDB.retrieve_labels(m.plant_meta['labels'], locales)

        labels[m.name] = m_labels

        variants = {}
        i = 0
        seasons = []
        s_labels = {}
        params_season = next(x for x in m.params if x['name'] == "season")['enum']
        for s in params_season['options']:
            seasons.append(s['name'])
            s_labels[s['name']] = ThicketDB.retrieve_labels(s['labels'], locales)
        default_season = params_season['default']

        for v in m.variants:
            v_rec = {}
            labels.update(s_labels)
            v_rec["index"] = i
            v_rec["seasons"] = seasons
            v_rec["default_season"] = seasons[default_season]
            preview_path = Path(filepath).parent.absolute() / "models" / (preview_stem + "_" + v.name + ".png")
            if not preview_path.is_file():
                logger.warning("Preview not found: %s" % preview_path)
                preview_path = ""
            v_rec["preview"] = str(preview_path)
            variants[v.name] = v_rec

            labels[v.name] = ThicketDB.retrieve_labels(next(x for x in params_variant['options'] if x['name'] == v.name)['labels'], locales)

            i = i + 1
        model["variants"] = variants

        m_rec["model"] = model
        m_rec["labels"] = labels
        return m_rec

    def parse_model_json(filepath, locales):
        m_rec = ThicketDB.parse_model(filepath, locales)
        print(json.dumps(m_rec))

    def retrieve_labels(labels, locales):
        result = {}

        # Store only the first label per locale
        for lang in locales:
            lbw_lang = lang
            if lbw_lang == "zh_HANS":  # Workaround: thicket and blender are using different strings here
                lbw_lang = "zh_CN"

            lbw_lang = lbw_lang.replace("_", "-")

            try:
                result[lang] = lbw.get_lang_string(labels, lbw_lang)
            except ValueError:
                logger.warning("Unable to fetch language string for language %s" % lbw_lang)

        return result


def main():
    global lbw, logger
    argParse = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                       description=textwrap.dedent('''\
Thicket Database Tool
Commands:
  read                read and print the db contents (requires -d)
  build               scan models path and add all models to a new db (requires -d -p -s)
  parse_model         read a model file and print the model record json (requires -f -s -t)
'''))

    argParse.add_argument("cmd", choices=["read", "build", "parse_model"])
    argParse.add_argument("-d", help="database filename")
    argParse.add_argument("-f", help="Laubwerk Model filename (lbw.gz)")
    argParse.add_argument("-l", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                          default="INFO", help="logger level")
    argParse.add_argument("-p", help="Laubwerk Models path")
    argParse.add_argument("-s", help="Laubwerk Python SDK path")
    argParse.add_argument("-t", help="Translations to fetch in form 'te-TE,te-TE,te-TE", type=lambda s: [item for item in s.split(',')])

    args = argParse.parse_args()

    logging.basicConfig(format="%(levelname)s: thicket_db: %(message)s", level=args.l)
    logger = logging.getLogger()

    if args.s:
        # If the SDK path was specified, attempt to import the Laubwerk SDK The
        # build and parse_model commands require the Laubwerk SDK which may or
        # may not be in the sys.path depending on the OS, environment, and how
        # it was called (from Blender, as a subprocess, or via the command
        # line).
        if args.s not in sys.path:
            sys.path.append(args.s)
        import laubwerk as lbw

    cmd = args.cmd
    if cmd == "read" and args.d:
        db = ThicketDB(args.d, create=False)
        db.read()
    elif cmd == "build" and args.d and args.p and lbw:
        db = ThicketDB(args.d, create=True)
        db.build(args.p, args.s)
    elif cmd == "parse_model" and args.f and lbw:
        ThicketDB.parse_model_json(args.f, args.t)
    else:
        argParse.print_help()
        return 1


if __name__ == "__main__":
    main()
