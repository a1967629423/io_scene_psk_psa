import bpy
from bpy.types import Operator, PropertyGroup, Action, UIList, BoneGroup, Panel, TimelineMarker
from bpy.props import CollectionProperty, IntProperty, FloatProperty, PointerProperty, StringProperty, BoolProperty, EnumProperty
from bpy_extras.io_utils import ExportHelper
from typing import Type
from .builder import PsaBuilder, PsaBuilderOptions
from .data import *
from ..types import BoneGroupListItem
from ..helpers import *
from collections import Counter
import re
import sys
import fnmatch


class PsaExporter(object):
    def __init__(self, psa: Psa):
        self.psa: Psa = psa

    # This method is shared by both PSA/K file formats, move this?
    @staticmethod
    def write_section(fp, name: bytes, data_type: Type[Structure] = None, data: list = None):
        section = Section()
        section.name = name
        if data_type is not None and data is not None:
            section.data_size = sizeof(data_type)
            section.data_count = len(data)
        fp.write(section)
        if data is not None:
            for datum in data:
                fp.write(datum)

    def export(self, path: str):
        with open(path, 'wb') as fp:
            self.write_section(fp, b'ANIMHEAD')
            self.write_section(fp, b'BONENAMES', Psa.Bone, self.psa.bones)
            self.write_section(fp, b'ANIMINFO', Psa.Sequence, list(self.psa.sequences.values()))
            self.write_section(fp, b'ANIMKEYS', Psa.Key, self.psa.keys)


class PsaExportActionListItem(PropertyGroup):
    action: PointerProperty(type=Action)
    name: StringProperty()
    is_selected: BoolProperty(default=False)


class PsaExportTimelineMarkerListItem(PropertyGroup):
    marker_index: IntProperty()
    name: StringProperty()
    is_selected: BoolProperty(default=True)


def update_action_names(context):
    pg = context.scene.psa_export
    for item in pg.action_list:
        action = item.action
        item.action_name = get_psa_sequence_name(action, pg.should_use_original_sequence_names)


def should_use_original_sequence_names_updated(property, context):
    update_action_names(context)


class PsaExportPropertyGroup(PropertyGroup):
    sequence_source: EnumProperty(
        name='Source',
        options=set(),
        description='',
        items=(
            ('ACTIONS', 'Actions', 'Sequences will be exported using actions', 'ACTION', 0),
            ('TIMELINE_MARKERS', 'Timeline Markers', 'Sequences will be exported using timeline markers', 'MARKER_HLT', 1),
        )
    )
    fps_source: EnumProperty(
        name='FPS Source',
        options=set(),
        description='',
        items=(
            ('SCENE', 'Scene', '', 'SCENE_DATA', 0),
            ('ACTION_METADATA', 'Action Metadata', 'The frame rate will be determined by action\'s "psa_fps" custom property, if it exists. If the Sequence Source is Timeline Markers, the lowest value of all contributing actions will be used. If no metadata is available, the scene\'s frame rate will be used.', 'PROPERTIES', 1),
            ('CUSTOM', 'Custom', '', 2)
        )
    )
    fps_custom: FloatProperty(default=30.0, min=sys.float_info.epsilon, soft_min=1.0, options=set(), step=100, soft_max=60.0)
    action_list: CollectionProperty(type=PsaExportActionListItem)
    action_list_index: IntProperty(default=0)
    marker_list: CollectionProperty(type=PsaExportTimelineMarkerListItem)
    marker_list_index: IntProperty(default=0)
    bone_filter_mode: EnumProperty(
        name='Bone Filter',
        options=set(),
        description='',
        items=(
            ('ALL', 'All', 'All bones will be exported.'),
            ('BONE_GROUPS', 'Bone Groups', 'Only bones belonging to the selected bone groups and their ancestors will '
                                           'be exported.'),
        )
    )
    bone_group_list: CollectionProperty(type=BoneGroupListItem)
    bone_group_list_index: IntProperty(default=0, name='', description='')
    should_use_original_sequence_names: BoolProperty(
        default=False,
        name='Original Names',
        options=set(),
        update=should_use_original_sequence_names_updated,
        description='If the action was imported from the PSA Import panel, the original name of the sequence will be '
                    'used instead of the Blender action name',
    )
    should_trim_timeline_marker_sequences: BoolProperty(
        default=True,
        name='Trim Sequences',
        options=set(),
        description='Frames without NLA track information at the boundaries of timeline markers will be excluded from '
                    'the exported sequences '
    )
    sequence_name_prefix: StringProperty(name='Prefix', options=set())
    sequence_name_suffix: StringProperty(name='Suffix', options=set())
    sequence_filter_name: StringProperty(default='', options={'TEXTEDIT_UPDATE'})
    sequence_use_filter_invert: BoolProperty(default=False, options=set())
    sequence_filter_asset: BoolProperty(default=False, name='Show assets', description='Show actions that belong to an asset library', options=set())
    sequence_use_filter_sort_reverse: BoolProperty(default=True, options=set())


def is_bone_filter_mode_item_available(context, identifier):
    if identifier == 'BONE_GROUPS':
        obj = context.active_object
        if not obj.pose or not obj.pose.bone_groups:
            return False
    return True


class PsaExportOperator(Operator, ExportHelper):
    bl_idname = 'psa_export.operator'
    bl_label = 'Export'
    bl_options = {'INTERNAL', 'UNDO'}
    __doc__ = 'Export actions to PSA'
    filename_ext = '.psa'
    filter_glob: StringProperty(default='*.psa', options={'HIDDEN'})
    filepath: StringProperty(
        name='File Path',
        description='File path used for exporting the PSA file',
        maxlen=1024,
        default='')

    def __init__(self):
        self.armature = None

    def draw(self, context):
        layout = self.layout
        pg = context.scene.psa_export

        # FPS
        layout.prop(pg, 'fps_source', text='FPS')
        if pg.fps_source == 'CUSTOM':
            layout.prop(pg, 'fps_custom', text='Custom')

        # SOURCE
        layout.prop(pg, 'sequence_source', text='Source')

        # SELECT ALL/NONE
        row = layout.row(align=True)
        row.label(text='Select')
        row.operator(PsaExportActionsSelectAll.bl_idname, text='All', icon='CHECKBOX_HLT')
        row.operator(PsaExportActionsDeselectAll.bl_idname, text='None', icon='CHECKBOX_DEHLT')

        # ACTIONS
        if pg.sequence_source == 'ACTIONS':
            rows = max(3, min(len(pg.action_list), 10))

            layout.template_list('PSA_UL_ExportActionList', '', pg, 'action_list', pg, 'action_list_index', rows=rows)

            col = layout.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(pg, 'should_use_original_sequence_names')
            col.prop(pg, 'sequence_name_prefix')
            col.prop(pg, 'sequence_name_suffix')

        elif pg.sequence_source == 'TIMELINE_MARKERS':
            rows = max(3, min(len(pg.marker_list), 10))
            layout.template_list('PSA_UL_ExportTimelineMarkerList', '', pg, 'marker_list', pg, 'marker_list_index', rows=rows)

            col = layout.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(pg, 'should_trim_timeline_marker_sequences')
            col.prop(pg, 'sequence_name_prefix')
            col.prop(pg, 'sequence_name_suffix')

        # Determine if there is going to be a naming conflict and display an error, if so.
        selected_items = [x for x in pg.action_list if x.is_selected]
        action_names = [x.name for x in selected_items]
        action_name_counts = Counter(action_names)
        for action_name, count in action_name_counts.items():
            if count > 1:
                layout.label(text=f'Duplicate action: {action_name}', icon='ERROR')
                break

        layout.separator()

        # BONES
        row = layout.row(align=True)
        row.prop(pg, 'bone_filter_mode', text='Bones')

        if pg.bone_filter_mode == 'BONE_GROUPS':
            row = layout.row(align=True)
            row.label(text='Select')
            row.operator(PsaExportBoneGroupsSelectAll.bl_idname, text='All', icon='CHECKBOX_HLT')
            row.operator(PsaExportBoneGroupsDeselectAll.bl_idname, text='None', icon='CHECKBOX_DEHLT')
            rows = max(3, min(len(pg.bone_group_list), 10))
            layout.template_list('PSX_UL_BoneGroupList', '', pg, 'bone_group_list', pg, 'bone_group_list_index', rows=rows)

    def should_action_be_selected_by_default(self, action):
        return action is not None and action.asset_data is None

    def is_action_for_armature(self, action):
        if len(action.fcurves) == 0:
            return False
        bone_names = set([x.name for x in self.armature.data.bones])
        for fcurve in action.fcurves:
            match = re.match(r'pose\.bones\["(.+)"\].\w+', fcurve.data_path)
            if not match:
                continue
            bone_name = match.group(1)
            if bone_name in bone_names:
                return True
        return False

    def invoke(self, context, event):
        pg = context.scene.psa_export

        if context.view_layer.objects.active is None:
            self.report({'ERROR_INVALID_CONTEXT'}, 'An armature must be selected')
            return {'CANCELLED'}

        if context.view_layer.objects.active.type != 'ARMATURE':
            self.report({'ERROR_INVALID_CONTEXT'}, 'The selected object must be an armature.')
            return {'CANCELLED'}

        self.armature = context.view_layer.objects.active

        # Populate actions list.
        pg.action_list.clear()
        for action in bpy.data.actions:
            if not self.is_action_for_armature(action):
                continue
            item = pg.action_list.add()
            item.action = action
            item.name = action.name
            item.is_selected = self.should_action_be_selected_by_default(action)

        update_action_names(context)

        # Populate timeline markers list.
        pg.marker_list.clear()
        for marker in context.scene.timeline_markers:
            item = pg.marker_list.add()
            item.name = marker.name

        if len(pg.action_list) == 0 and len(pg.marker_list) == 0:
            # If there are no actions at all, we have nothing to export, so just cancel the operation.
            self.report({'ERROR_INVALID_CONTEXT'}, 'There are no actions or timeline markers to export.')
            return {'CANCELLED'}

        # Populate bone groups list.
        populate_bone_group_list(self.armature, pg.bone_group_list)

        context.window_manager.fileselect_add(self)

        return {'RUNNING_MODAL'}

    def execute(self, context):
        pg = context.scene.psa_export

        actions = [x.action for x in pg.action_list if x.is_selected]
        marker_names = [x.name for x in pg.marker_list if x.is_selected]

        options = PsaBuilderOptions()
        options.fps_source = pg.fps_source
        options.fps_custom = pg.fps_custom
        options.sequence_source = pg.sequence_source
        options.actions = actions
        options.marker_names = marker_names
        options.bone_filter_mode = pg.bone_filter_mode
        options.bone_group_indices = [x.index for x in pg.bone_group_list if x.is_selected]
        options.should_use_original_sequence_names = pg.should_use_original_sequence_names
        options.should_trim_timeline_marker_sequences = pg.should_trim_timeline_marker_sequences
        options.sequence_name_prefix = pg.sequence_name_prefix
        options.sequence_name_suffix = pg.sequence_name_suffix

        builder = PsaBuilder()

        try:
            psa = builder.build(context, options)
        except RuntimeError as e:
            self.report({'ERROR_INVALID_CONTEXT'}, str(e))
            return {'CANCELLED'}

        exporter = PsaExporter(psa)
        exporter.export(self.filepath)
        return {'FINISHED'}


class PSA_UL_ExportTimelineMarkerList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, 'is_selected', icon_only=True, text=item.name)

    def filter_items(self, context, data, property):
        pg = context.scene.psa_export
        sequences = getattr(data, property)
        flt_flags = filter_sequences(pg, sequences)
        flt_neworder = bpy.types.UI_UL_list.sort_items_by_name(sequences, 'name')
        return flt_flags, flt_neworder


def filter_sequences(pg: PsaExportPropertyGroup, sequences: bpy.types.bpy_prop_collection) -> List[int]:
    bitflag_filter_item = 1 << 30
    flt_flags = [bitflag_filter_item] * len(sequences)

    if pg.sequence_filter_name is not None:
        # Filter name is non-empty.
        for i, sequence in enumerate(sequences):
            if not fnmatch.fnmatch(sequence.name, f'*{pg.sequence_filter_name}*'):
                flt_flags[i] &= ~bitflag_filter_item

    if not pg.sequence_filter_asset:
        for i, sequence in enumerate(sequences):
            if hasattr(sequence, 'action') and sequence.action.asset_data is not None:
                flt_flags[i] &= ~bitflag_filter_item

    if pg.sequence_use_filter_invert:
        # Invert filter flags for all items.
        for i, sequence in enumerate(sequences):
            flt_flags[i] ^= bitflag_filter_item

    return flt_flags


def get_visible_sequences(pg: PsaExportPropertyGroup, sequences: bpy.types.bpy_prop_collection) -> List[PsaExportActionListItem]:
    visible_sequences = []
    for i, flag in enumerate(filter_sequences(pg, sequences)):
        if bool(flag & (1 << 30)):
            visible_sequences.append(sequences[i])
    return visible_sequences


class PSA_UL_ExportActionList(UIList):

    def __init__(self):
        super(PSA_UL_ExportActionList, self).__init__()
        # Show the filtering options by default.
        self.use_filter_show = True

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, 'is_selected', icon_only=True, text=item.name)
        if item.action.asset_data is not None:
            layout.label(text='', icon='ASSET_MANAGER')

    def draw_filter(self, context, layout):
        pg = context.scene.psa_export
        row = layout.row()
        subrow = row.row(align=True)
        subrow.prop(pg, 'sequence_filter_name', text="")
        subrow.prop(pg, 'sequence_use_filter_invert', text="", icon='ARROW_LEFTRIGHT')
        subrow = row.row(align=True)
        subrow.prop(pg, 'sequence_filter_asset', icon_only=True, icon='ASSET_MANAGER')
        # subrow.prop(pg, 'sequence_use_filter_sort_reverse', text='', icon='SORT_ASC')

    def filter_items(self, context, data, property):
        pg = context.scene.psa_export
        actions = getattr(data, property)
        flt_flags = filter_sequences(pg, actions)
        flt_neworder = bpy.types.UI_UL_list.sort_items_by_name(actions, 'name')
        return flt_flags, flt_neworder


class PsaExportActionsSelectAll(Operator):
    bl_idname = 'psa_export.sequences_select_all'
    bl_label = 'Select All'
    bl_description = 'Select all visible sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def get_item_list(cls, context):
        pg = context.scene.psa_export
        if pg.sequence_source == 'ACTIONS':
            return pg.action_list
        elif pg.sequence_source == 'TIMELINE_MARKERS':
            return pg.marker_list
        return None

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_export
        item_list = cls.get_item_list(context)
        visible_sequences = get_visible_sequences(pg, item_list)
        has_unselected_sequences = any(map(lambda item: not item.is_selected, visible_sequences))
        return has_unselected_sequences

    def execute(self, context):
        pg = context.scene.psa_export
        sequences = self.get_item_list(context)
        for sequence in get_visible_sequences(pg, sequences):
            sequence.is_selected = True
        return {'FINISHED'}


class PsaExportActionsDeselectAll(Operator):
    bl_idname = 'psa_export.sequences_deselect_all'
    bl_label = 'Deselect All'
    bl_description = 'Deselect all visible sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def get_item_list(cls, context):
        pg = context.scene.psa_export
        if pg.sequence_source == 'ACTIONS':
            return pg.action_list
        elif pg.sequence_source == 'TIMELINE_MARKERS':
            return pg.marker_list
        return None

    @classmethod
    def poll(cls, context):
        item_list = cls.get_item_list(context)
        has_selected_items = any(map(lambda item: item.is_selected, item_list))
        return len(item_list) > 0 and has_selected_items

    def execute(self, context):
        pg = context.scene.psa_export
        item_list = self.get_item_list(context)
        for sequence in get_visible_sequences(pg, item_list):
            sequence.is_selected = False
        return {'FINISHED'}


class PsaExportBoneGroupsSelectAll(Operator):
    bl_idname = 'psa_export.bone_groups_select_all'
    bl_label = 'Select All'
    bl_description = 'Select all bone groups'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_export
        item_list = pg.bone_group_list
        has_unselected_items = any(map(lambda action: not action.is_selected, item_list))
        return len(item_list) > 0 and has_unselected_items

    def execute(self, context):
        pg = context.scene.psa_export
        for item in pg.bone_group_list:
            item.is_selected = True
        return {'FINISHED'}


class PsaExportBoneGroupsDeselectAll(Operator):
    bl_idname = 'psa_export.bone_groups_deselect_all'
    bl_label = 'Deselect All'
    bl_description = 'Deselect all bone groups'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_export
        item_list = pg.bone_group_list
        has_selected_actions = any(map(lambda action: action.is_selected, item_list))
        return len(item_list) > 0 and has_selected_actions

    def execute(self, context):
        pg = context.scene.psa_export
        for action in pg.bone_group_list:
            action.is_selected = False
        return {'FINISHED'}


classes = (
    PsaExportActionListItem,
    PsaExportTimelineMarkerListItem,
    PsaExportPropertyGroup,
    PsaExportOperator,
    PSA_UL_ExportActionList,
    PSA_UL_ExportTimelineMarkerList,
    PsaExportActionsSelectAll,
    PsaExportActionsDeselectAll,
    PsaExportBoneGroupsSelectAll,
    PsaExportBoneGroupsDeselectAll,
)
