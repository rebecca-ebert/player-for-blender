# Thicket: Laubwerk Plants Add-on for Blender
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
# This project was forked from and inspired by:
#   https://bitbucket.org/laubwerk/lbwbl
#
# Copyright (C) 2015 Fabian Quosdorf <fabian@faqgames.net>
# Copyright (C) 2019-2020 Darren Hart <dvhart@infradead.org>


# <pep8 compliant>

"""Thicket: Laubwerk Plants Add-on for Blender

Thicket adds import and level-of-detail support to Blender for Laubwerk Plant
Kits. It requires the Laubwerk Python SDK included with all Laubwerk Plant Kits.
"""

import logging
from pathlib import Path, PurePath
import platform
import sys
import time

import bpy
from bpy.types import (AddonPreferences,
                       Operator,
                       Panel,
                       PropertyGroup
                       )
from bpy.props import (BoolProperty,
                       EnumProperty,
                       FloatProperty,
                       IntProperty,
                       PointerProperty,
                       StringProperty,
                       )
from bpy.app.translations import locales, locale_explode
import bpy.utils.previews


bl_info = {
    "name": "Thicket: Laubwerk Plants Add-on for Blender",
    "author": "Darren Hart",
    "version": (0, 4, 1),
    "blender": (2, 80, 0),
    "location": "View3D > Sidebar > Thicket",
    "description": "Import Laubwerk Plants (.lbw.gz)",
    "warning": "This is an unofficial development release",
    "wiki_url": "https://github.com/Thicket-Blender/thicket/blob/master/README.md",
    "tracker_url": "https://github.com/Thicket-Blender/thicket/issues",
    "support": 'COMMUNITY',
    "category": "Import-Export"
}


# Create a thicket specific logger which logs to a file and propogates messages to the root logger.
logger = logging.getLogger(__name__)
log_path = Path(bpy.utils.user_resource('SCRIPTS', path="addons", create=True)) / __name__ / "thicket.log"
log_handler = logging.FileHandler(log_path, encoding=None, mode='a', delay=False)
log_formatter = logging.Formatter('%(asctime)s: %(levelname)s: %(message)s')
log_handler.setFormatter(log_formatter)
logger.addHandler(log_handler)


class ThicketStatus:
    lbw_models_valid = False
    lbw_sdk_valid = False
    imported = False
    ready = False


thicket_status = ThicketStatus()
db = None
thicket_previews = None
thicket_ui_mode = 'VIEW'
thicket_ui_obj = None
THICKET_GUID = '5ff1c66f282a45a488a6faa3070152a2'
THICKET_SCALE = 10


###############################################################################
# Thicket helper functions
#
# These are mostly functions that are used by more than one class. Placed here
# at the top for name resolution purposes.
###############################################################################


def populate_previews():
    """Create a Blender preview collection of model thumbnails

    Walk through all the models in the Thicket database and add the thumbnails
    for each model and each model variant to the previews collection for use in
    the model properties panels.

    Previews are keyed on the model name and variant as well as just the model
    name as a fall back. In case no previews are available, the
    "missing_preview" key points to a generic preview.
    """

    global db, thicket_previews

    if thicket_previews:
        bpy.utils.previews.remove(thicket_previews)
    thicket_previews = bpy.utils.previews.new()

    t0 = time.time()

    thicket_path = Path(bpy.utils.user_resource('SCRIPTS', path="addons", create=True)) / __name__
    missing_path = thicket_path / "doc" / "missing_preview.png"
    thicket_previews.load("missing_preview", str(missing_path), 'IMAGE')
    multiple_path = thicket_path / "doc" / "multiple_preview.png"
    thicket_previews.load("multiple_preview", str(multiple_path), 'IMAGE')

    for model in db:
        # Load the top model (no variant) preview
        model_preview_key = model.name.replace(" ", "_").replace(".", "")
        preview_path = model.preview
        if preview_path != "" and Path(preview_path).is_file():
            thicket_previews.load(model_preview_key, preview_path, 'IMAGE')

        # Load the previews for each variant of the model
        for variant in model.variants:
            preview_key = model_preview_key + "_" + variant.name
            preview_path = variant.preview
            if preview_path != "" and Path(preview_path).is_file():
                thicket_previews.load(preview_key, preview_path, 'IMAGE')

    logger.debug("Added %d previews in %0.2fs" % (len(thicket_previews), time.time()-t0))


def get_preview(model_name, variant=""):
    """Lookup model variant preview

    Return the best match from best to worst:
        * model and variant
        * model
        * missing_preview

    Parameters
    ----------
    model_name : str
        The name of the model from the db or Laubwerk model.name
    variant : str
        The name of the model variant from the db or Laubwerk variant.name

    Returns
    -------
    preview
    """

    preview_key = model_name.replace(" ", "_").replace(".", "")
    if variant != "":
        preview_key = model_name.replace(" ", "_").replace(".", "") + "_" + variant
        if preview_key not in thicket_previews:
            # The variant specific preview was not found, try the model preview
            logger.debug("Preview key %s not found" % preview_key)
            preview_key = model_name.replace(" ", "_").replace(".", "")
    if preview_key not in thicket_previews:
        logger.debug("Preview key %s not found" % preview_key)
        preview_key = "missing_preview"
    return thicket_previews[preview_key]


def thicket_init():
    """Import dependencies and setup globals

    Thicket depends on the Laubwerk Python Extension (SDK). The user needs to
    configure the Laubwerk installation paths via the Thicket Addon Preferences.
    This function restricts functionality until the setup is complete.

    Check the Laubwerk installation paths are valid and import the laubwerk
    modules and the thicket components dependent on the laubwerk module.

    Setup the database and populate the preview catalog.

    Parameters
    ----------
    none

    Returns
    -------
    none
    """

    global thicket_status, db, ThicketDB, thicket_lbw, laubwerk, logger

    if db is not None:
        bpy.app.translations.unregister(__name__)

    thicket_status = ThicketStatus()
    db = None

    prefs = bpy.context.preferences.addons[__name__].preferences
    if "log_level" not in prefs.keys():
        prefs.log_level = 'INFO'
    logger.setLevel(prefs.log_level)

    if prefs.lbw_models_path != "" and Path(prefs.lbw_models_path).is_dir():
        logger.info("Laubwerk Models Path: '%s'" % prefs.lbw_models_path)
        thicket_status.lbw_models_valid = True
    else:
        logger.warning("Invalid Laubwerk Models Path: '%s'" % prefs.lbw_models_path)

    if prefs.lbw_sdk_path != "" and Path(prefs.lbw_sdk_path).is_dir():
        logger.info("Laubwerk Python Extension Path: '%s'" % prefs.lbw_sdk_path)
    else:
        logger.warning("Invalid Laubwerk Python Extension Path: '%s'" % prefs.lbw_sdk_path)
        return

    if str(prefs.lbw_sdk_path) not in sys.path:
        sys.path.append(str(prefs.lbw_sdk_path))

    try:
        import laubwerk
    except ImportError:
        logger.critical("Failed to load laubwerk module")
        return

    try:
        from . import thicket_lbw
    except ImportError:
        logger.critical("Failed to load thicket_lbw")
        return
    thicket_status.lbw_sdk_valid = True

    if not thicket_status.lbw_models_valid:
        return

    try:
        from .thicket_db import ThicketDB, ThicketDBOldSchemaError
    except ImportError:
        logger.critical("Failed to import thicket_db.ThicketDB")
        return

    thicket_status.imported = True
    logger.info(laubwerk.version)

    # capture all available languages and process them to the form "language_country"
    availableLocales = []
    for locale in locales:
        explodedLocale = locale_explode(locale)
        localeString = explodedLocale[3]
        if not localeString:
            localeString = explodedLocale[0]

        availableLocales.append(localeString)

    db_path = Path(bpy.utils.user_resource('SCRIPTS', path="addons", create=True)) / __name__ / "thicket.db"
    try:
        db = ThicketDB(db_path, availableLocales, sys.executable)
    except ThicketDBOldSchemaError:
        logger.warning("Old database schema found, creating empty database")
        db_path.unlink()
    except FileNotFoundError:
        logger.info("Database not found, creating empty database")

    if db is None or db.model_count() == 0:
        db_dir = Path(PurePath(db_path).parent)
        db_dir.mkdir(parents=True, exist_ok=True)
        db = ThicketDB(db_path, availableLocales, sys.executable, True)
        return

    bpy.app.translations.register(__name__, db.get_translation_dict())

    populate_previews()

    thicket_status.ready = True
    logger.info("Database (%d models): %s" % (db.model_count(), db_path))
    logger.info("Ready")


def is_thicket_instance(obj):
    """Check if the object is a Thicket instance

    Thicket instances point to an instance_collection containing a
    ThicketPropGroup (thicket) with the magic property set to THICKET_GUID.

    Avoid attempting to work with Thicket object before thicket_init has been
    called successfully by requiring thicket_status to be READY.

    Parameters
    ----------
    obj : Object
        Typically bpy.context.active_object

    Returns
    -------
    Boolean
    """

    if not thicket_status.ready:
        return False

    if obj and obj.instance_collection and obj.instance_collection.thicket.magic == THICKET_GUID:
        return True
    return False


def delete_model_template(template):
    """Delete a Thicket model template with 0 users

    If there are 0 users, unlink (and optionally remove) all the objects in a
    Thicket model collection, remove the collection, and remove any data items
    left with 0 users (saving the user a save/reload operation to clear them
    out.)

    Parameters
    ----------
    template : Collection

    Returns
    -------
    none
    """

    if len(template.users_dupli_group) == 0:
        for o in template.objects:
            template.objects.unlink(o)
            if o.users == 0:
                bpy.data.objects.remove(o)
        bpy.data.collections.remove(template, do_unlink=True)

        for d in [d for d in bpy.data.meshes if d.users == 0]:
            bpy.data.meshes.remove(d)
        for d in [d for d in bpy.data.materials if d.users == 0]:
            bpy.data.materials.remove(d)
        for d in [d for d in bpy.data.images if d.users == 0]:
            bpy.data.images.remove(d)


def delete_model(instance):
    """Delete a Thicket model instance

    Remove the instance and the template if this is the last user.

    Parameters
    ----------
    instance : Object (Collection Instance)

    Returns
    -------
    none
    """

    template = instance.instance_collection
    bpy.data.objects.remove(instance, do_unlink=True)
    delete_model_template(template)


def select_model(filepath, defaults=False):
    """Setup the UI ThicketPropGroup with the specified model

    Paramaters
    ----------
    filepath : String
        thicket_db filepath key to the desired model
    defaults : Boolean
        Use the model defaults (True) or keep the current selection for variant
        and season (or model defaults if not set or unavailable)

    Returns
    -------
    none
    """

    global db

    tp = bpy.context.window_manager.thicket
    model = db.get_model(filepath=filepath)

    # Store the old values and set the variant and season to the 0 entry (should always exist)
    old_variant = tp.variant
    old_season = tp.season

    if defaults:
        keys = list(tp.keys())
        for key in keys:
            tp.pop(key)

    tp.name = model.name
    if tp.batch_mode:
        tp.batch_name = tp.name

    # Restore the old values if available, others reset to the defaults
    variant = model.get_variant(old_variant)
    tp.variant = variant.name
    tp.season = variant.get_season(old_season).name


################################################################################
# Thicket Blender classes
#
# Subclasses of Blender objects, such as PropertyGroup, Operators, and Panels
################################################################################


class ThicketPropGroup(PropertyGroup):
    """Thicket model properties

    These properties identify the Laubwerk model by name as well as all the
    parameters used to generate the mesh. These are attached to the model
    collection template and bpy.types.WindowManager as "thicket".
    """
    def __eq__(self, other):
        for k, v in self.items():
            try:
                if self[k] != other[k]:
                    return False
            except KeyError:
                return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def eq_lod(self, other):
        for k, v in self.items():
            if k not in ["leaf_density", "use_lod_max_level", "lod_max_level", "use_lod_min_thick", "lod_min_thick",
                         "lod_subdiv", "leaf_amount"]:
                continue
            try:
                if self[k] != other[k]:
                    return False
            except KeyError:
                return False
        return True

    def copy_to(self, other):
        for k, v in self.items():
            other[k] = v

    def import_lbw(self, original=None):
        filepath = db.get_model(name=self.name).filepath
        tp = self
        mesh_args = {}
        mesh_args["variant"] = self.variant
        mesh_args["season"] = self.season

        if original:
            orig_template = original.instance_collection
            orig_tp = orig_template.thicket

        if self.batch_mode:
            if self.batch_name == "":
                filepath = db.get_model(name=orig_tp.name).filepath
            else:
                filepath = db.get_model(name=self.batch_name).filepath
            if not self.batch_use_lod:
                tp = orig_tp
            mesh_args["variant"] = self.batch_variant
            if self.batch_variant == 'UNCHANGED':
                mesh_args["variant"] = orig_tp.variant
            mesh_args["season"] = self.batch_season
            if self.batch_season == 'UNCHANGED':
                mesh_args["season"] = orig_tp.season

        mesh_args["leaf_density"] = tp.leaf_density / 100.0
        if tp.use_lod_max_level:
            mesh_args["max_branch_level"] = tp.lod_max_level
        if tp.use_lod_min_thick:
            mesh_args["min_thickness"] = tp.lod_min_thick
        mesh_args["max_subdiv_level"] = tp.lod_subdiv
        mesh_args["leaf_amount"] = tp.leaf_amount / 100.0

        viewport_obj = None
        render_obj = None

        # Determine if either the FULL render object or LOW viewport object can
        # be reused to save regenerating those meshes.  Do not attempt to avoid
        # regenerating proxy objects as these are fast enough.
        if original and self.name == orig_tp.name and self.variant == orig_tp.variant and \
           self.season == orig_tp.season:
            if self.eq_lod(orig_tp):
                if self.render_lod == orig_tp.render_lod:
                    render_obj = orig_template.objects[-1]

            if self.viewport_lod != self.render_lod:
                if self.viewport_lod == 'PROXY' and self.viewport_lod == orig_tp.viewport_lod:
                    viewport_obj = orig_template.objects[0]

            if self.render_lod == 'PROXY' and orig_tp.viewport_lod == 'PROXY':
                if self.render_lod != orig_tp.render_lod:
                    render_obj = orig_template.objects[0]

        model_obj = thicket_lbw.import_lbw(filepath, tp.viewport_lod, tp.render_lod, mesh_args,
                                           viewport_obj, render_obj)
        self.copy_to(model_obj.instance_collection.thicket)
        model_obj.instance_collection.thicket.magic = THICKET_GUID
        return model_obj

    def variant_callback(self, context):
        global db, thicket_ui_mode

        tp = context.window_manager.thicket
        if thicket_ui_mode == 'VIEW':
            tp = context.active_object.instance_collection.thicket
        model = db.get_model(name=tp.name)
        items = []

        if not model:
            items.append(('DEFAULT', "default", ""))
        else:
            for v in model.variants:
                items.append((v.name, v.label, ""))
        return items

    def season_callback(self, context):
        global db, thicket_ui_mode

        tp = context.window_manager.thicket
        if thicket_ui_mode == 'VIEW':
            tp = context.active_object.instance_collection.thicket

        model = db.get_model(name=tp.name)
        items = []

        if not model:
            items.append(("default", "default", ""))
        else:
            for s in model.get_variant(tp.variant).seasons:
                items.append((s.name, s.label, ""))
        return items

    def batch_variant_callback(self, context):
        global db
        variants = ['01young', '01medium', '01adult',
                    '02young', '02medium', '02adult',
                    '03young', '03medium', '03adult']
        return [(v, db.get_label(v), "") for v in variants] + [('UNCHANGED', "--", "")]

    def batch_season_callback(self, context):
        global db
        seasons = ['spring', 'summer', 'fall', 'winter']
        return [(s, db.get_label(s), "") for s in seasons] + [('UNCHANGED', "--", "")]

    def render_lod_update(self, context):
        if self.render_lod == 'PROXY':
            self.viewport_lod = 'PROXY'

    # name is provided by the PropertyGroup and used to store the unique Laubwerk Model name
    magic: bpy.props.StringProperty()
    variant: EnumProperty(items=variant_callback, name="Variant")
    season: EnumProperty(items=season_callback, name="Season")
    leaf_density: FloatProperty(name="Leaf Density", description="How full the foliage appears",
                                default=100.0, min=0.01, max=100.0, subtype='PERCENTAGE')
    viewport_lod: EnumProperty(name="Viewport",
                               items=[('PROXY', "Proxy", ""),
                                      ('LOW', "Partial Geometry", ""),
                                      ('FULL', "Full Geometry", "")],
                               default='PROXY')
    render_lod: EnumProperty(name="Render", description="Render level of detail",
                             items=[('PROXY', "Proxy", ""),
                                    ('FULL', "Full Geometry", "")],
                             default='FULL', update=render_lod_update)
    use_lod_max_level: BoolProperty(name="", description="Manually specify Max Branching Level", default=False)
    lod_max_level: IntProperty(name="Max Branching Level", description="Max branching levels off the trunk",
                               default=5, min=0, max=10, step=1)
    use_lod_min_thick: BoolProperty(name="", description="Manually specify Min Branch Thickness", default=False)
    lod_min_thick: FloatProperty(name="Min Branch Thickness", description="Min thickness of trunk or branches",
                                 default=0.1, min=0.1, max=10000.0, step=1.0)
    lod_subdiv: IntProperty(name="Max Subdivisions", description="How round the trunk and branches appear",
                            default=1, min=0, max=5, step=1)
    leaf_amount: FloatProperty(name="Leaf Amount", description="How many leaves used for leaf density "
                               "(smaller number means larger leaves)",
                               default=100.0, min=0.01, max=100.0, subtype='PERCENTAGE')

    # These batch properties are not derived from a specific model, but instead assume
    # the standard variant and season options available for all Laubwerk models.
    batch_mode: BoolProperty(default=False)
    batch_name: StringProperty(default="")
    batch_variant: EnumProperty(name="Variant", items=batch_variant_callback)
    batch_season: EnumProperty(name="Season", items=batch_season_callback)
    batch_use_lod: BoolProperty(name="Show Geometry Options",
                                description="Show options affecting geometry for selected models.",
                                default=False)


class THICKET_OT_reset_model(Operator):
    """Reset UI model properties to original"""

    bl_idname = "thicket.reset_model"
    bl_label = "Reset Model"
    bl_description = "Restore the UI properties to the variant properties"
    bl_options = {'REGISTER', 'INTERNAL'}

    next_mode: StringProperty()

    def execute(self, context):
        global thicket_ui_mode
        instance = context.active_object
        if not is_thicket_instance(instance):
            logger.error("reset_model failed: non-Thicket object: %s" % instance.name)
            return
        template = instance.instance_collection
        template.thicket.copy_to(context.window_manager.thicket)
        thicket_ui_mode = self.next_mode
        context.area.tag_redraw()
        return {'FINISHED'}


# Thicket operator to modify (delete and replace) the backing objects
class THICKET_OT_update_model(Operator):
    """Update the model with the new properties

    Regenerate the template model using the UI properties and point
    all the instances to the new template, and remove the original.
    """

    bl_idname = "thicket.update_model"
    bl_label = "Update Model"
    bl_description = "Update model with new properties"
    bl_options = {'REGISTER', 'INTERNAL'}

    next_mode: StringProperty()

    def update_model(self, instance, tp):
        logger.debug("Updating model: %s" % instance.name)
        template = instance.instance_collection

        # Load new model variant
        new_instance = tp.import_lbw(instance)
        new_template = new_instance.instance_collection

        # Update the instance_collection reference in the instances
        for i in template.users_dupli_group:
            i.instance_collection = new_template
            i.name = new_template.name

        # Remove the new instance collection and the old template
        delete_model(new_instance)
        delete_model_template(template)

    def execute(self, context):
        global thicket_ui_mode
        active = context.active_object
        if not is_thicket_instance(active):
            logger.error("update_model failed: non-Thicket active object: %s" % active.name)
            return

        models = context.selected_objects
        templates = []
        for m in models:
            logger.debug("update_model: updating %s" % m.name)
            if not is_thicket_instance(m):
                logger.debug("update_model: skipped non-Thicket object: %s" % m.name)
                continue
            if m.instance_collection not in templates:
                self.update_model(m, context.window_manager.thicket)
                templates.append(m.instance_collection)

        for m in models:
            m.select_set(True)

        # Restore the active object
        bpy.context.view_layer.objects.active = active

        context.area.tag_redraw()
        thicket_ui_mode = self.next_mode
        return {'FINISHED'}


# Thicket make unique operator
class THICKET_OT_make_unique(Operator):
    """Make the active model be the only user of a new model template

    Duplicate the model template of the active instance and point the
    instance_collection to the new template. The active instance will now be the
    only user of a new model template. If its properties are changed, only the
    one instance will be updated.
    """

    bl_idname = "thicket.make_unique"
    bl_label = "Make Unique"
    bl_description = "Display number of models using this template (click to make unique)"
    bl_options = {'REGISTER', 'INTERNAL'}

    def make_unique(self, instance):
        template = instance.instance_collection
        if len(template.users_dupli_group) == 1:
            logger.warning("%s already is unique" % instance.name)
            return

        # Create a copy of the template and use the new one
        new_template = template.copy()
        bpy.data.collections["Thicket"].children.link(new_template)
        instance.instance_collection = new_template

    def execute(self, context):
        active = context.active_object
        if not is_thicket_instance(active):
            logger.error("make_unique failed: non-Thicket active object: %s" % active.name)
            return

        models = context.selected_objects
        for m in models:
            logger.debug("make_unique: %s" % m.name)
            if not is_thicket_instance(m):
                logger.debug("make_unique: skipped non-Thicket object: %s" % m.name)
                continue
            self.make_unique(m)

        for m in models:
            m.select_set(True)

        # Restore the active object
        bpy.context.view_layer.objects.active = active

        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_OT_delete_model(Operator):
    """Delete the active model instance and the template if it is the last user"""

    bl_idname = "thicket.delete_model"
    bl_label = "Delete"
    bl_description = "Delete the active model and remove the template if there are no instances remaining"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        models = context.selected_objects
        objects = []
        for m in models:
            logger.debug("delete_model: %s" % m.name)
            if not is_thicket_instance(m):
                logger.debug("delete_model: skipped non-Thicket object: %s" % m.name)
                objects.append(m)
                continue
            delete_model(m)

        for o in objects:
            o.select_set(True)

        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_OT_select_model(Operator):
    """Change the model being added or edited"""

    bl_idname = "thicket.select_model"
    bl_label = "Select"
    bl_descroption = "Change the model of the active object"
    bl_options = {'REGISTER', 'INTERNAL'}

    filepath: StringProperty(subtype='FILE_PATH')
    next_mode: StringProperty()

    def execute(self, context):
        global thicket_ui_mode
        # If adding a new model, start off with the defaults
        defaults = self.next_mode == 'ADD'
        select_model(self.filepath, defaults)
        thicket_ui_mode = self.next_mode

        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_OT_change_mode(Operator):
    """Select a new model for the UI"""

    bl_idname = "thicket.change_mode"
    bl_label = "Change Model"
    bl_description = "Change the Thicket Sidebar to display model selection"
    bl_options = {'REGISTER', 'INTERNAL'}

    next_mode: StringProperty()

    def execute(self, context):
        global db, thicket_ui_mode
        thicket_ui_mode = self.next_mode

        # If there is no UI ThicketPropGroup setup, select the first model in the DB
        if thicket_ui_mode == 'ADD' and context.window_manager.thicket.name == '':
            filepath = next(iter(db)).filepath
            select_model(filepath, True)

        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_OT_edit_model(Operator):
    """Copy the active model properties to the window_manager.thicket properties"""

    bl_idname = "thicket.edit_model"
    bl_label = "Edit"
    bl_description = "Edit the active model"
    bl_options = {'REGISTER', 'INTERNAL'}

    next_mode: StringProperty()
    batch_mode: BoolProperty(default=False)

    @classmethod
    def poll(self, context):
        return is_thicket_instance(context.active_object)

    def execute(self, context):
        global thicket_ui_mode, thicket_ui_obj
        thicket_ui_obj = context.active_object
        context.active_object.instance_collection.thicket.copy_to(context.window_manager.thicket)

        tp = context.window_manager.thicket
        tp.batch_mode = self.batch_mode
        tp.batch_name = ""
        tp.batch_variant = 'UNCHANGED'
        tp.batch_season = 'UNCHANGED'

        thicket_ui_mode = self.next_mode
        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_OT_load_model(Operator):
    """Load a model into the scene with the current properties"""

    bl_idname = "thicket.load_model"
    bl_label = "Add"
    bl_description = "Load a model into the scene with the current properties"""
    bl_options = {'REGISTER', 'INTERNAL'}

    next_mode: StringProperty()

    def execute(self, context):
        global thicket_ui_mode
        tp = context.window_manager.thicket
        thicket_ui_obj = tp.import_lbw()
        thicket_ui_obj.instance_collection.thicket.copy_to(context.window_manager.thicket)
        thicket_ui_mode = self.next_mode
        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_OT_clear_search(Operator):
    """Select a new model for the UI"""

    bl_idname = "thicket.clear_search"
    bl_label = "Clear Search"
    bl_description = "Clear the Thicket search string"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        context.window_manager.thicket_search = ""
        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_PT_model_properties(Panel):
    """Thicket Model Properties Panel

    Sidebar panel to display the properties of the active model. It displays a
    delete and make unique button, followed by a thumbnail and all the
    properties from the ThicketPropGroup, along with a reset and update button
    to restore the properties to the original state or regenerate the template
    model and updating all models using that same template.
    """

    # bl_idname = self.type
    bl_label = "Thicket Model Properties"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Thicket"

    def next_mode(self, op):
        global thicket_ui_mode
        # modes: ADD, EDIT, SELECT, SELECT_ADD, VIEW
        ops = ['ADD', 'CANCEL', 'CHANGE', 'CONFIRM', 'DELETE', 'EDIT', 'MAKE_UNIQUE']
        m = thicket_ui_mode
        nm = m

        if op not in ops:
            logger.error("Unknown ui mode transition operator: %s" % (op))
            return nm

        if m == 'ADD':
            if op == 'CANCEL':
                nm = 'VIEW'
            elif op == 'CHANGE':
                nm = 'SELECT_ADD'
            elif op == 'CONFIRM':
                nm = 'VIEW'
            elif op == 'DELETE':
                nm = 'VIEW'
        elif m == 'EDIT':
            if op == 'ADD':
                nm = 'SELECT_ADD'
            if op == 'CANCEL':
                nm = 'VIEW'
            elif op == 'CHANGE':
                nm = 'SELECT'
            elif op == 'CONFIRM':
                nm = 'VIEW'
            elif op == 'DELETE':
                nm = 'VIEW'
        elif m == 'SELECT':
            if op == 'CANCEL':
                nm = 'EDIT'
            elif op == 'CONFIRM':
                nm = 'EDIT'
        elif m == 'SELECT_ADD':
            if op == 'CANCEL':
                nm = 'ADD'
            elif op == 'CONFIRM':
                nm = 'ADD'
        elif m == 'VIEW':
            if op == 'ADD':
                nm = 'ADD'
            elif op == 'EDIT':
                nm = 'EDIT'

        return nm

    def draw_gallery(self, context):
        global THICKET_SCALE
        layout = self.layout
        panel_w = context.region.width
        cell_w = int(THICKET_SCALE * bpy.app.render_icon_size)
        num_cols = max(1, int((float(panel_w) / cell_w) - 0.5))
        o = layout.operator("thicket.change_mode", text="Cancel")
        o.next_mode = self.next_mode('CANCEL')

        # Search box to filter on name and common name (label)
        r = layout.row()
        r.prop(context.window_manager, "thicket_search", icon='VIEWZOOM', text="")
        r.operator("thicket.clear_search", text="", icon='CANCEL')

        grid = layout.grid_flow(columns=num_cols, even_columns=True, even_rows=False)
        for model in db:
            search = context.window_manager.thicket_search.lower()
            name = model.name
            label = model.label
            if search not in name.lower() and search not in label.lower():
                continue
            cell = grid.column().box()
            cell.template_icon(icon_value=get_preview(name).icon_id, scale=THICKET_SCALE)
            cell.label(text="%s" % label)
            cell.label(text="(%s)" % name)
            o = cell.operator("thicket.select_model")
            o.filepath = model.filepath
            o.next_mode = self.next_mode('CONFIRM')
        o = layout.operator("thicket.change_mode", text="Cancel")
        o.next_mode = self.next_mode('CANCEL')

    def draw_props(self, layout, tp, batch=False):
        """Draw the model properties UI"""

        if not batch:
            layout.prop(tp, "variant")
            layout.prop(tp, "season")
        else:
            layout.prop(tp, "batch_variant")
            layout.prop(tp, "batch_season")
            layout.prop(tp, "batch_use_lod")
            if not tp.batch_use_lod:
                return

        layout.prop(tp, "leaf_density")

        layout.separator()

        layout.label(text="Level of Detail")
        r = layout.row()
        r.enabled = not tp.render_lod == 'PROXY'
        r.prop(tp, "viewport_lod")
        layout.prop(tp, "render_lod")

        c = layout.column()
        c.enabled = tp.render_lod == 'FULL'

        r = c.row()
        c2 = r.column()
        c2.prop(tp, "use_lod_max_level")
        c2 = r.column()
        c2.enabled = tp.use_lod_max_level
        c2.prop(tp, "lod_max_level")

        r = c.row()
        c2 = r.column()
        c2.prop(tp, "use_lod_min_thick")
        c2 = r.column()
        c2.enabled = tp.use_lod_min_thick
        c2.prop(tp, "lod_min_thick")

        c.prop(tp, "lod_subdiv")
        c.prop(tp, "leaf_amount")

    def draw(self, context):
        global db, thicket_status, thicket_ui_mode, thicket_ui_obj, THICKET_SCALE

        layout = self.layout

        # Check for Thicket initialization problems
        if not thicket_status.ready:
            if thicket_status.imported:
                layout.label(text="Please rebuild the database")
                layout.operator("thicket.rebuild_db", icon="FILE_REFRESH")
            else:
                layout.label(text="See Thicket Add-on Preferences")
            return

        template = None
        siblings = 0
        tp = None

        models = []
        templates = []
        for m in context.selected_objects:
            if is_thicket_instance(m):
                models.append(m)
                if m.instance_collection not in templates:
                    templates.append(m.instance_collection)
                    t_siblings = len(m.instance_collection.users_dupli_group)
                    if t_siblings > 1:
                        siblings += t_siblings
        model_count = len(models)
        batch = model_count > 1

        instance = context.active_object
        if instance is not thicket_ui_obj:
            thicket_ui_obj = None
            if thicket_ui_mode == 'EDIT':
                thicket_ui_mode = 'VIEW'

        if (instance and is_thicket_instance(instance)):
            thicket_ui_obj = instance
            template = instance.instance_collection

        if thicket_ui_mode == 'VIEW':
            if template:
                tp = template.thicket
        else:
            tp = context.window_manager.thicket

        if thicket_ui_mode in ['SELECT', 'SELECT_ADD']:
            self.draw_gallery(context)
            return

        # Draw Add, Delete, and Make Unique in VIEW mode only
        if thicket_ui_mode == 'VIEW':
            o = layout.operator("thicket.change_mode", text="Add Model")
            o.next_mode = self.next_mode('ADD')
            if model_count == 0:
                return
            layout.operator("thicket.delete_model", icon='NONE', text="Delete (%d)" % model_count)
            r = layout.row()
            r.operator("thicket.make_unique", icon='NONE', text="Make Unique (%d)" % siblings)
            r.enabled = siblings > 1
            layout.separator()

        # Determine the model name and preview based on the active model or the
        # model chosen from Change Model if in batch mode.
        model = None
        preview = get_preview("missing_preview", "")
        if batch:
            layout.label(text="Multiple Models (%d)" % model_count)
            preview = get_preview("multiple_preview", "")
            if tp and not tp.batch_name == "":
                model = db.get_model(name=tp.batch_name)
        else:
            if tp is None:
                return
            model = db.get_model(name=tp.name)

            if model is None:
                layout.label(text="Model not found in database")
                layout.operator("thicket.rebuild_db", icon="FILE_REFRESH")
                return

        if model:
            layout.label(text="%s" % model.label)
            layout.label(text="(%s)" % model.name)
            preview = get_preview(model.name, tp.batch_variant if batch else tp.variant)

        layout.template_icon(preview.icon_id, scale=THICKET_SCALE)

        if thicket_ui_mode == 'VIEW':
            o = layout.operator("thicket.edit_model")
            o.next_mode = self.next_mode('EDIT')
            o.batch_mode = batch
            if batch:
                return
        elif thicket_ui_mode in ['ADD', 'EDIT']:
            o = layout.operator("thicket.change_mode")
            o.next_mode = self.next_mode('CHANGE')

        # Draw the model properties
        col = layout.column()
        col.enabled = thicket_ui_mode != 'VIEW'
        self.draw_props(col, tp, batch)

        if thicket_ui_mode == 'VIEW':
            v = template.all_objects[0].data
            r = template.all_objects[len(template.all_objects) - 1].data
            layout.separator()
            layout.label(text="Viewport: Verts:%s | Faces:%s" % (f"{len(v.vertices):,}",  f"{len(v.polygons):,}"))
            layout.label(text="Render: Verts:%s | Faces:%s" % (f"{len(r.vertices):,}",  f"{len(r.polygons):,}"))
            return

        # Draw the confirm and cancel buttons
        layout.separator()
        r = layout.row()
        if thicket_ui_mode == 'EDIT':
            c = r.column()
            o = c.operator("thicket.reset_model", icon="NONE", text="Cancel")
            o.next_mode = self.next_mode('CANCEL')
            c = r.column()
            o = c.operator("thicket.update_model", icon="NONE", text="Update")
            o.next_mode = self.next_mode('CONFIRM')
            c.enabled = tp != template.thicket
        elif thicket_ui_mode == 'ADD':
            c = r.column()
            o = c.operator("thicket.change_mode", text="Cancel")
            o.next_mode = self.next_mode('CANCEL')
            c = r.column()
            o = c.operator("thicket.load_model")
            o.next_mode = self.next_mode('CONFIRM')


class THICKET_OT_rebuild_db(Operator):
    """Rebuild the Thicket database from the installed Laubwerk Plant Kits

    Create a new database, adding all the models found in the Laubwerk install
    path. This will take some time depending on the configuration of the
    computer. One model parsing process is spawned for every available CPU.
    """

    bl_idname = "thicket.rebuild_db"
    bl_label = "Rebuild Database"
    bl_description = "Process Laubwerk Plants library and update the database (may take several minutes)"
    bl_options = {'REGISTER', 'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        global db
        logger.info("Rebuilding database, this may take several minutes...")
        t0 = time.time()
        prefs = context.preferences.addons[__name__].preferences
        db.build(str(prefs.lbw_models_path), str(prefs.lbw_sdk_path))
        logger.info("Rebuilt database in %0.2fs" % (time.time()-t0))
        thicket_init()
        context.area.tag_redraw()
        return {'FINISHED'}


class THICKET_Pref(AddonPreferences):
    """Thicket Addon Preference Panel

    Configure the location of the Laubwerk installation paths and rebuild the
    database for the first time, or after installing a new Laubwerk Plant Pack.
    """

    bl_idname = __name__

    default_models_path = ""
    default_sdk_path = ""
    if platform.system() == "Windows":
        default_models_path = "C:\\Program Files\\Laubwerk\\Plants"
        default_sdk_path = "C:\\Program Files\\Laubwerk\\Python"
    if platform.system() == "Darwin":
        default_models_path = "/Library/Application Support/Laubwerk/Plants"
        default_sdk_path = "/Library/Application Support/Laubwerk/Python"

    def lbw_path_on_update(self, context):
        if self.lbw_sdk_path != "":
            self["lbw_sdk_path"] = str(Path(bpy.path.abspath(self.lbw_sdk_path)).resolve())
            logger.debug("Absolute Laubwerk Python Extension path: '%s'" % self.lbw_sdk_path)
        if self.lbw_models_path != "":
            self["lbw_models_path"] = str(Path(bpy.path.abspath(self.lbw_models_path)).resolve())
            logger.debug("Absolute Laubwerk Models path: '%s'" % self.lbw_models_path)
        if self.lbw_sdk_path != "" and self.lbw_models_path != "":
            thicket_init()

    lbw_sdk_path: StringProperty(
        name="Python Extension",
        subtype="DIR_PATH",
        description="absolute path to Laubwerk Python Extension directory (default: %s)" % default_sdk_path,
        default=default_sdk_path,
        update=lbw_path_on_update
        )

    lbw_models_path: StringProperty(
        name="Models",
        subtype="DIR_PATH",
        description="absolute path to Laubwerk Models directory (default: %s)" % default_models_path,
        default=default_models_path,
        update=lbw_path_on_update
        )

    log_level: EnumProperty(name="Log Level",
                            items=[('DEBUG', "Debug", "", logging.DEBUG),
                                   ('INFO', "Info", "", logging.INFO),
                                   ('WARNING', "Warning", "", logging.WARNING),
                                   ('ERROR', "Error", "", logging.ERROR),
                                   ('CRITICAL', "Critical", "", logging.CRITICAL)],
                            default='INFO')

    def draw(self, context):
        global db, thicket_status

        box = self.layout.box()
        box.label(text="Laubwerk Installation")

        row = box.row()
        col = row.column()
        col.alert = not thicket_status.lbw_models_valid
        col.prop(self, "lbw_models_path")
        if col.alert:
            col.label(text="Path is not a directory")

        row = box.row()
        col = row.column()
        col.alert = not thicket_status.lbw_sdk_valid
        col.prop(self, "lbw_sdk_path")
        if col.alert:
            col.label(text="Failed to load Laubwerk Python Extension from this path")

        lbw_version = "Laubwerk Version: N/A"
        if thicket_status.lbw_sdk_valid:
            lbw_version = laubwerk.version

        db_status = "Please rebuild the database"
        if thicket_status.ready:
            db_status = "Database contains %d models" % db.model_count()

        box.label(text=lbw_version)
        row = box.row()
        row.alert = thicket_status.imported and not thicket_status.ready
        col = row.column()
        col.label(text=db_status)
        col = row.column()
        col.enabled = thicket_status.imported
        col.operator("thicket.rebuild_db", icon="FILE_REFRESH")

        box = self.layout.box()
        box.label(text="Advanced Settings")
        box.prop(self, "log_level")


__classes__ = (
        THICKET_Pref,
        THICKET_OT_rebuild_db,
        THICKET_OT_reset_model,
        THICKET_OT_update_model,
        THICKET_OT_delete_model,
        THICKET_OT_make_unique,
        THICKET_OT_change_mode,
        THICKET_OT_select_model,
        THICKET_OT_edit_model,
        THICKET_OT_load_model,
        THICKET_OT_clear_search,
        ThicketPropGroup,
        THICKET_PT_model_properties
)


def register():
    """Thicket Add-on Blender register"""

    for c in __classes__:
        bpy.utils.register_class(c)

    bpy.types.Collection.thicket = PointerProperty(type=ThicketPropGroup)
    bpy.types.WindowManager.thicket = PointerProperty(type=ThicketPropGroup)
    bpy.types.WindowManager.thicket_search = StringProperty(description="Filter by botanical or common name")

    thicket_init()


def unregister():
    """Thicket Add-on Blender unregister"""

    global thicket_previews

    if thicket_previews:
        bpy.utils.previews.remove(thicket_previews)
    for c in reversed(__classes__):
        bpy.utils.unregister_class(c)

    bpy.app.translations.unregister(__name__)


if __name__ == "__main__":
    register()
