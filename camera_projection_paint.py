"""
Camera Projection Paint Module

Main module for camera projection painting workflow.
"""

import bpy
import traceback
import os
from bpy.types import PropertyGroup, Panel, Operator
from bpy.props import (
    PointerProperty,
    BoolProperty,
    StringProperty,
    CollectionProperty,
    IntProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
)

from bpy.app.handlers import persistent
from . import uv_bake_eevee
from . import common
from . import psd_watcher
from . import psd_handler

# =====================================================================
# HANDLERS
# =====================================================================
@persistent
def on_load_post(dummy):
    """Handler called after a blend file is loaded"""
    try:
        scene = bpy.context.scene
        if not hasattr(scene, 'cam_proj_paint'):
            return
        
        # Auto-start watchdog if enabled
        if scene.cam_proj_paint.auto_reload_enabled:
            if psd_watcher.WATCHDOG_AVAILABLE:
                if psd_watcher.start_watching_psd_file(scene):
                    print("Camera Projection Paint: Auto-reload enabled - watching PSD file for changes")
                else:
                    print("Camera Projection Paint: Failed to start auto-reload on file load")
            else:
                print("Camera Projection Paint: Watchdog not available - cannot start auto-reload")
    except Exception as e:
        print(f"Camera Projection Paint: Error in load_post handler: {e}")

@persistent
def on_projection_psd_reload(scene, depsgraph):
    """Automatically apply projection image when it's reloaded"""
    try:
        projection_psd = scene.cam_proj_paint.projection_psd_file
        if not projection_psd:
            return
        
        # Check if the projection psd was updated
        for update in depsgraph.updates:
            if hasattr(update, 'id') and update.id == projection_psd:
                print(f"Projection psd '{projection_psd.name}' was reloaded, applying to objects...")
                # Use a timer to defer the apply operation slightly
                bpy.app.timers.register(lambda: apply_projection_delayed(), first_interval=0.5)
                break
    except Exception as e:
        # Silently fail to avoid spamming console
        pass
    
# Auto-reload handler for projection image
def apply_projection_delayed():
    """Delayed apply to ensure image is fully loaded"""
    try:
        enabled_objects = common.get_enabled_objects(bpy.context)
        if enabled_objects:
            bpy.ops.camprojpaint.reload_projection_image()
            print(f"Applied projection image to {len(enabled_objects)} enabled object(s)")
    except Exception as e:
        print(f"Error applying projection image after reload: {e}")
    return None  # Don't repeat timer

# =====================================================================
# UTILITY FUNCTIONS
# =====================================================================

def parse_ignore_prefixes(scene):
    """Parse the comma-separated ignore prefixes from the scene property.

    Returns a list of lower-cased prefixes with whitespace trimmed. Empty items
    are removed. If the property is empty, returns an empty list.
    """
    try:
        raw = scene.cam_proj_paint.ignore_material_prefixes
    except Exception:
        return []

    return common.parse_comma_separated_list(raw, lowercase=True, strip_whitespace=True)

def get_mat_data_by_id(obj, slot_id):
    if not hasattr(obj, 'cam_proj_paint') or not obj.cam_proj_paint:
        return None
    if not obj.cam_proj_paint.material_data:
        return None
    
    for md in obj.cam_proj_paint.material_data:
        if md.material_index == slot_id:
            return md

def get_mat_data_by_og_mat_name(obj, mat_name):
    if not hasattr(obj, 'cam_proj_paint') or not obj.cam_proj_paint:
        return None
    if not obj.cam_proj_paint.material_data:
        return None
    
    for md in obj.cam_proj_paint.material_data:
        if md.original_material_name == mat_name:
            return md

def get_psd_layer_items(self, context):
    """Dynamic enum items for PSD layer selection"""
    items = [('NONE', "None", "No PSD layer assigned", 'X', 0)]
    
    scene = context.scene
    if not scene or not hasattr(scene, 'cam_proj_paint'):
        return items
    
    settings = scene.cam_proj_paint
    if not settings.projection_psd_file:
        return items
    
    if not os.path.exists(settings.projection_psd_file):
        return items
    
    # Get PSD layers
    try:
        psd_layers = psd_handler.get_psd_layer_list(settings.projection_psd_file)
        if psd_layers:
            for idx, (layer_name, is_group) in enumerate(psd_layers):
                if not is_group:  # Only show non-group layers
                    # Use idx + 1 to avoid conflict with NONE (0)
                    icon = 'OUTLINER_DATA_GP_LAYER'
                    items.append((layer_name, layer_name, f"PSD Layer: {layer_name}", icon, idx + 1))
    except Exception as e:
        print(f"Failed to get PSD layers for enum: {e}")
    
    return items

# =====================================================================
# PROPERTY CALLBACKS
# =====================================================================

def on_psd_layer_enum_update(self, context):
    """Update callback when PSD layer enum changes"""
    if self.psd_layer_enum == 'NONE':
        self.psd_layer_name = ""
    else:
        self.psd_layer_name = self.psd_layer_enum
        
    # Reload this specific texture node with the new layer
    reload_texture_node(self, context)

def on_projection_filter_update(self, context):
    """Update callback when projection filter settings change"""
    obj = context.active_object
    if not obj or not hasattr(obj, 'cam_proj_paint'):
        return
    
    # Find the material data that contains this texture node
    mat_data = None
    for md in obj.cam_proj_paint.material_data:
        for tn in md.texture_nodes:
            if tn == self:
                mat_data = md
                break
        if mat_data:
            break
    
    if not mat_data:
        return
    
    # Update preview material filter node if it exists
    preview_mat = bpy.data.materials.get(mat_data.preview_material_name)
    if preview_mat and preview_mat.use_nodes and self.projection_filter_mix_node_name:
        nodes = preview_mat.node_tree.nodes
        filter_node = nodes.get(self.projection_filter_mix_node_name)
        
        if filter_node:
            # Update blend type
            if hasattr(filter_node, 'blend_type'):
                if self.projection_filter_type == 'NONE':
                    filter_node.blend_type = 'MIX'
                else:
                    filter_node.blend_type = self.projection_filter_type
            
            # Update color input
            if filter_node.type == 'MIX':
                # New Mix node (Blender 3.4+)
                filter_node.inputs[7].default_value = self.projection_filter_color
            else:
                # Legacy MixRGB node
                filter_node.inputs['Color2'].default_value = self.projection_filter_color
            
            # Update factor based on filter type
            if self.projection_filter_type == 'NONE':
                # Set factor to 0 to bypass the filter
                if filter_node.type == 'MIX':
                    filter_node.inputs[0].default_value = 0.0
                else:
                    filter_node.inputs['Fac'].default_value = 0.0
            else:
                # Set factor to 1 to apply the filter
                if filter_node.type == 'MIX':
                    filter_node.inputs[0].default_value = 1.0
                else:
                    filter_node.inputs['Fac'].default_value = 1.0
    
    # Update bake material filter node if it exists
    bake_mat = bpy.data.materials.get(self.bake_material_name)
    if bake_mat and bake_mat.use_nodes and self.projection_filter_mix_node_name:
        nodes = bake_mat.node_tree.nodes
        filter_node = nodes.get(self.projection_filter_mix_node_name)
        
        if filter_node:
            # Update blend type
            if hasattr(filter_node, 'blend_type'):
                if self.projection_filter_type == 'NONE':
                    filter_node.blend_type = 'MIX'
                else:
                    filter_node.blend_type = self.projection_filter_type
            
            # Update color input
            if filter_node.type == 'MIX':
                # New Mix node (Blender 3.4+)
                filter_node.inputs[7].default_value = self.projection_filter_color
            else:
                # Legacy MixRGB node
                filter_node.inputs['Color2'].default_value = self.projection_filter_color
            
            # Update factor based on filter type
            if self.projection_filter_type == 'NONE':
                # Set factor to 0 to bypass the filter
                if filter_node.type == 'MIX':
                    filter_node.inputs[0].default_value = 0.0
                else:
                    filter_node.inputs['Fac'].default_value = 0.0
            else:
                # Set factor to 1 to apply the filter
                if filter_node.type == 'MIX':
                    filter_node.inputs[0].default_value = 1.0
                else:
                    filter_node.inputs['Fac'].default_value = 1.0

def on_enabled_update(self, context):
    """Update callback when object enabled state changes"""
    # Find the object that owns this property
    obj = None
    for o in bpy.data.objects:
        if hasattr(o, 'cam_proj_paint') and o.cam_proj_paint == self:
            obj = o
            break
    
    if not obj:
        return
    
    if self.enabled:
        # When enabling, update material data
        ignore_prefixes = parse_ignore_prefixes(context.scene)
        ensure_obj_material_data(obj, ignore_prefixes)
    else:
        # When disabling, restore original materials
        restore_original_materials(obj)

def on_projection_image_update(self, context):
    """Update callback when projection image changes - automatically apply to all objects"""
    if self.projection_image:
        if common.get_enabled_objects(context):
            # Apply the projection image to all enabled objects
            bpy.ops.camprojpaint.reload_projection_image()

# =====================================================================
# CORE FUNCTIONS
# =====================================================================

def ensure_obj_material_data(obj, ignore_prefixes=None, rediscover_tex_nodes=False):
    """
    Scan the object's materials for image texture nodes and create material data.
    
    Args:
        obj: The object to scan for texture nodes
        ignore_prefixes: List of material name prefixes to ignore (optional)
    """
    if not obj.data.materials:
        return
    
    print(f"\nObject: {obj.name}")
    
    for mat_slot_idx, mat_slot in enumerate(obj.material_slots):
        mat = mat_slot.material
        if not mat:
            continue
        
        # Skip preview and bake materials (temporary materials created by this addon)
        if mat.name.endswith(common.PREVIEW_MAT_SUFFIX) or mat.name.endswith(common.BAKE_MAT_SUFFIX):
            print(f"  Skipping material '{mat.name}' (preview/bake material)")
            continue
        
        # Skip materials with ignored prefixes
        if ignore_prefixes and common.match_prefixes(mat.name, ignore_prefixes):
            print(f"  Skipping material '{mat.name}' (ignored prefix)")
            continue
        
        if not mat.use_nodes:
            print(f"  Material '{mat.name}': No nodes enabled")
            continue
        
        # Find or create material data entry
        mat_data = get_mat_data_by_og_mat_name(obj, mat.name)
        
        if not mat_data:
            mat_data = obj.cam_proj_paint.material_data.add()
            mat_data.material_index = mat_slot_idx
            mat_data.original_material_name = mat.name
        else:
            # Update mat data
            mat_data.material_index = mat_slot_idx
            mat_data.original_material_name = mat.name
        
        # Find all image texture nodes
        texture_nodes = common.find_all_image_texture_nodes(mat)
        
        if not texture_nodes:
            print(f"  Material '{mat.name}': No image texture nodes found")
            continue
        
        print(f"  Material '{mat.name}': Found {len(texture_nodes)} texture node(s)")
        
        # Build texture node data entries if not already present
        if mat_data.texture_nodes and not rediscover_tex_nodes:
            return
        
        # Record existing psd layer mappings before clearing
        existing_mappings = {}
        if mat_data.texture_nodes:
            for tex_node_data in mat_data.texture_nodes:
                existing_mappings[tex_node_data.node_name] = tex_node_data.psd_layer_name
            
        # Clear existing texture node data
        mat_data.texture_nodes.clear()
        
        # Get a list of psd layer enum items
        psd_enums = get_psd_layer_items(None, bpy.context)
        
        for tex_node in texture_nodes:
            # Create texture node data entry
            tex_data = mat_data.texture_nodes.add()
            tex_data.node_name = tex_node.name
            tex_data.original_texture = tex_node.image
            
            # Get connected UV map
            uv_map = common.get_connected_uv_map(tex_node)
            tex_data.original_uv_map = uv_map if uv_map else ""
            
            # Try to restore existing PSD layer mapping
            if tex_node.name in existing_mappings:
                tex_data.psd_layer_name = existing_mappings[tex_node.name]
                for enum_item in psd_enums:
                    if enum_item[0] == tex_data.psd_layer_name:
                        tex_data.psd_layer_enum = enum_item[0]
                        break
            else: 
                # Initialize PSD layer to empty (user will map manually)
                tex_data.psd_layer_name = ""
            
            img_name = tex_node.image.name if tex_node.image else "None"
            uv_info = f", UV: {tex_data.original_uv_map}" if tex_data.original_uv_map else ""
            print(f"    - Node: '{tex_node.name}', Image: '{img_name}'{uv_info}")

    # Go through all the material data and remove ones whose materials are no longer in object's materials
    for md_id, mat_data in reversed(list(enumerate(obj.cam_proj_paint.material_data))):
        # Get the og mat/preview mat recorded in the mat data entry
        og_mat = bpy.data.materials.get(mat_data.original_material_name)
        preview_mat = bpy.data.materials.get(mat_data.preview_material_name)
        if not og_mat or og_mat.name not in obj.data.materials:
            if not preview_mat or preview_mat.name not in obj.data.materials:
                print(f"  Removing material data for missing material '{mat_data.original_material_name}'")
                obj.cam_proj_paint.material_data.remove(md_id)

def auto_map_psd_layers_to_textures(context, psd_file_path, enabled_objects, verbose=True):
    """
    Automatically map PSD layers to texture nodes by matching image names.
    
    Args:
        context: Blender context
        psd_file_path: Path to PSD file
        enabled_objects: List of enabled objects to process
        verbose: If True, print detailed progress
    
    Returns:
        tuple: (mapped_count, total_nodes)
    """
    if not psd_handler.is_psd_available():
        if verbose:
            print("psd-tools not available")
        return 0, 0
    
    if not os.path.exists(psd_file_path):
        if verbose:
            print(f"PSD file not found: {psd_file_path}")
        return 0, 0
    
    # Get PSD layers
    psd_layers = psd_handler.get_psd_layer_list(psd_file_path)
    if not psd_layers:
        if verbose:
            print("No PSD layers found")
        return 0, 0
    
    # Create lookup dict of layer names (case-insensitive)
    # Extract just the layer name without group path (e.g., "Group/Layer" -> "Layer")
    layer_lookup = {}
    for layer_name, is_group in psd_layers:
        if not is_group:
            # Get just the layer name without the group path
            simple_name = layer_name.split('/')[-1]
            layer_lookup[simple_name.lower()] = layer_name
    
    mapped_count = 0
    total_nodes = 0
    
    for obj in enabled_objects:
        for mat_data in obj.cam_proj_paint.material_data:
            for tex_node_data in mat_data.texture_nodes:
                total_nodes += 1
                
                # Try to match by image name (without extension)
                if tex_node_data.original_texture:
                    img_name = tex_node_data.original_texture.name
                    
                    # Remove common image extensions
                    for ext in ['.png', '.jpg', '.jpeg', '.tga', '.exr', '.hdr', '.tif', '.tiff']:
                        if img_name.lower().endswith(ext):
                            img_name = img_name[:-len(ext)]
                            break
                    
                    # Try exact match first
                    if img_name in layer_lookup:
                        layer_name = layer_lookup[img_name]
                        tex_node_data.psd_layer_name = layer_name
                        tex_node_data.psd_layer_enum = layer_name
                        mapped_count += 1
                        if verbose:
                            print(f"  ✓ Mapped '{tex_node_data.node_name}' → '{layer_lookup[img_name]}'")
                    # Try case-insensitive match
                    elif img_name.lower() in layer_lookup:
                        layer_name = layer_lookup[img_name.lower()]
                        tex_node_data.psd_layer_name = layer_name
                        tex_node_data.psd_layer_enum = layer_name
                        mapped_count += 1
                        if verbose:
                            print(f"  ✓ Mapped '{tex_node_data.node_name}' → '{layer_lookup[img_name.lower()]}' (case-insensitive)")
                    else:
                        if verbose:
                            print(f"  ✗ No match for '{tex_node_data.node_name}' (image: '{img_name}')")
    
    return mapped_count, total_nodes

def setup_obj_projection_uv_and_visibility(obj, camera, context):
    """
    Setup projection UV maps and visibility vertex colors for a single object.
    
    Args:
        obj: The object to setup projection UVs for
        camera: The camera to project from
        context: Blender context
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Clear Mirror Motion accumulated motion before UV projection/visibility calculation
        if hasattr(obj, 'mirror_motion') and obj.mirror_motion.enabled:
            try:
                # Attempt to import the mirror_motion module functions
                import sys
                mirror_motion_module = sys.modules.get('mirror_motion')
                if mirror_motion_module:
                    clear_func = getattr(mirror_motion_module, 'clear_motion_and_history_object', None)
                    if clear_func:
                        clear_func(obj.mirror_motion, obj)
                        print(f"  {obj.name}: Cleared Mirror Motion accumulated motion")
            except Exception as e:
                print(f"  {obj.name}: Warning - Could not clear Mirror Motion: {e}")
        
        # Clear Mirror Motion for armature bones if this object is being controlled by bones
        if obj.type == 'ARMATURE':
            try:
                import sys
                mirror_motion_module = sys.modules.get('mirror_motion')
                if mirror_motion_module:
                    clear_func = getattr(mirror_motion_module, 'clear_motion_and_history_bone', None)
                    if clear_func:
                        for bone in obj.pose.bones:
                            if hasattr(bone, 'mirror_motion') and bone.mirror_motion.enabled:
                                clear_func(bone.mirror_motion, obj, bone)
                                print(f"  {obj.name}/{bone.name}: Cleared Mirror Motion accumulated motion")
            except Exception as e:
                print(f"  {obj.name}: Warning - Could not clear bone Mirror Motion: {e}")
        
        # Store the current 3D viewport perspective from the active area
        original_perspective = None
        space_3d = None
        
        if context.area and context.area.type == 'VIEW_3D':
            space_3d = context.space_data
            if space_3d and hasattr(space_3d, 'region_3d'):
                original_perspective = space_3d.region_3d.view_perspective
        
        # Set viewport to camera view
        if space_3d and hasattr(space_3d, 'region_3d') and space_3d.region_3d:
            space_3d.region_3d.view_perspective = 'CAMERA'
        
         # Store original selection and active object
        selection_state = common.store_selection_state(context)
        
        # Deselect all objects
        bpy.ops.object.select_all(action='DESELECT')
        
        # Select and make active
        obj.select_set(True)
        context.view_layer.objects.active = obj
        
        # Ensure the object has at least one UV map
        if not common.has_uv_layers(obj):
            obj.data.uv_layers.new(name="UVMap")

        # Store original active UV map
        original_uv_name = common.get_active_uv_layer_name(obj)
        obj.cam_proj_paint.original_uv = original_uv_name

        # Ensure the original object has the projection UV map (destination)
        dest_proj_uv = common.ensure_uv_layer(obj, common.PROJECTION_UV_NAME, make_active=True)
        if dest_proj_uv:
            print(f"  {obj.name}: Ensured '{common.PROJECTION_UV_NAME}' UV map on target")

        # Duplicate the object and apply deforming modifiers on the duplicate
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.duplicate()
        duplicate_obj = context.active_object
        if duplicate_obj == obj:
            raise RuntimeError("Failed to duplicate object for UV projection")
        duplicate_obj.name = f"{obj.name}_UV_Helper"
        print(f"  {obj.name}: Created helper duplicate '{duplicate_obj.name}'")

        # Remove modifiers that generate new geometry from the duplicate
        uv_bake_eevee.remove_generate_modifiers(duplicate_obj)

        # Convert the duplicate to a mesh to apply deformation modifiers
        try:
            bpy.ops.object.convert(target='MESH')
        except Exception:
            # In some contexts convert may fail; try switching to object mode first
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.convert(target='MESH')
            except Exception as e:
                raise
        duplicate_obj = context.active_object

        # On the duplicate, create/use the projection UV and project from view
        src_proj_uv = common.ensure_uv_layer(duplicate_obj, common.PROJECTION_UV_NAME, make_active=True)
        if src_proj_uv:
            print(f"  {obj.name}: Ensured '{common.PROJECTION_UV_NAME}' UV map on helper")

        # Enter edit mode on duplicate and project UVs from camera view
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.project_from_view(
            camera_bounds=True,
            correct_aspect=False,
            scale_to_bounds=False
        )
        bpy.ops.object.mode_set(mode='OBJECT')

        # Transfer UVs from duplicate (source) to original (target) using topology mapping
        try:
            # Ensure both objects are selected and duplicate is active (source)
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            duplicate_obj.select_set(True)
            context.view_layer.objects.active = duplicate_obj

            # Transfer UVs first
            bpy.ops.object.data_transfer(
                data_type='UV',
                loop_mapping='TOPOLOGY',
                poly_mapping='TOPOLOGY'
            )
            print(f"  {obj.name}: Transferred UVs from helper to target")

            # Calculate visibility on the deformed duplicate so occlusion/facing
            # is evaluated in deformed space, then transfer the vertex color
            # layer back to the original using topology mapping.
            try:
                # Determine fill value based on projection mode
                projection_mode = obj.cam_proj_paint.projection_mode
                if projection_mode == 'PRESERVE':
                    # Skip visibility calculation - preserve existing mask
                    print(f"  {obj.name}: Skipped visibility calculation (mode: PRESERVE)")
                elif projection_mode == 'VISIBLE':
                    fill_value = None  # Use camera-facing calculation
                    common.calculate_camera_visibility(duplicate_obj, camera, fill=fill_value)
                    print(f"  {obj.name}: Calculated visibility on helper (mode: {projection_mode})")
                elif projection_mode == 'ALL':
                    fill_value = 1.0  # Fill all surfaces
                    common.calculate_camera_visibility(duplicate_obj, camera, fill=fill_value)
                    print(f"  {obj.name}: Calculated visibility on helper (mode: {projection_mode})")
            except Exception as e:
                print(f"  {obj.name}: Failed to calculate visibility on helper: {e}")

            # Transfer vertex color (Projection_Visibility) from helper to original
            try:
                # Skip transfer if projection mode is PRESERVE
                projection_mode = obj.cam_proj_paint.projection_mode
                if projection_mode == 'PRESERVE':
                    print(f"  {obj.name}: Skipped vertex color transfer (mode: PRESERVE)")
                else:
                    # Add a new vertex color layer on the original if it doesn't exist
                    if not obj.data.vertex_colors.get(common.PROJECTION_VIS_VCOL_NAME):
                        obj.data.vertex_colors.new(name=common.PROJECTION_VIS_VCOL_NAME)
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    duplicate_obj.select_set(True)
                    context.view_layer.objects.active = duplicate_obj

                    bpy.ops.object.data_transfer(
                        data_type='COLOR_CORNER',
                        loop_mapping='TOPOLOGY',
                        poly_mapping='TOPOLOGY',
                    )
                    print(f"  {obj.name}: Transferred vertex colors (Projection_Visibility) from helper to target")
            except Exception as e:
                print(f"  {obj.name}: Vertex color transfer failed: {e}")

        except RuntimeError as e:
            print(f"  {obj.name}: UV data transfer failed: {e}")
            # Cleanup duplicate and continue
            common.remove_object(duplicate_obj)
            raise

        # Remove the duplicate helper object
        common.remove_object(duplicate_obj)

        # Restore original UV as active if it existed
        if original_uv_name and obj.data.uv_layers.get(original_uv_name):
            obj.data.uv_layers.active = obj.data.uv_layers[original_uv_name]

        # Restore the original viewport perspective
        if space_3d and space_3d.region_3d and original_perspective:
            space_3d.region_3d.view_perspective = original_perspective

        # Restore original selection and active object
        common.restore_selection_state(context, selection_state)

        # Store the current frame number when UVs and visibility were created
        obj.cam_proj_paint.projection_frame = context.scene.frame_current

        print(f"  {obj.name}: ✓ Setup complete (original UV: '{original_uv_name}', frame: {context.scene.frame_current})")
        return True
        
    except Exception as e:
        # Restore the original viewport perspective even on error
        try:
            if 'space_3d' in locals() and space_3d and space_3d.region_3d and 'original_perspective' in locals() and original_perspective:
                space_3d.region_3d.view_perspective = original_perspective
        except:
            pass
        
        # Restore original selection even on error
        try:
            if 'selection_state' in locals() and selection_state:
                common.restore_selection_state(context, selection_state)
        except:
            pass
        
        print(f"  {obj.name}: ✗ Error - {str(e)}")
        return False

def setup_projection_mix(mat, og_img_node, proj_img, context, dest_socket=None, tex_node_data=None):
    """Setup the projection mix nodes in the given material.

    Args:
        mat (Material): The material to setup.
        og_img_node (ShaderNodeTexImage): The original image texture node that will be mixed with the projection image texture node.
        proj_img (Image): The projection image to use.
        context: The Blender context object for driver setup.
        dest_socket (NodeSocket, optional): The destination socket to connect the mix result to. If None, defaults to material output color input.
        tex_node_data (CAMPROJPAINT_TextureNodeData, optional): Texture node data containing filter settings.

    Returns:
        (None, None) if setup failed, otherwise the (name of projection texture node, name of projection filter node).
    """
    if not mat or not mat.use_nodes:
        return None
    
    if not og_img_node or og_img_node.type != 'TEX_IMAGE':
        return None
    
    try:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        # Get the original image node's position for reference
        og_x, og_y = og_img_node.location
        
        # Generate unique node names based on the original texture node
        base_name = og_img_node.name
        
        # Create UV Map node for projection UV
        projection_uv_node = nodes.new(type='ShaderNodeUVMap')
        projection_uv_node.name = f"{common.NODE_NAME_PROJECTION_UV}__{base_name}"
        projection_uv_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_UV)
        projection_uv_node.uv_map = common.PROJECTION_UV_NAME
        projection_uv_node.location = (og_x - 400, og_y - 400)
        
        # Create Projection texture node
        projection_tex_node = nodes.new(type='ShaderNodeTexImage')
        projection_tex_node.name = f"{common.NODE_NAME_PROJECTION_TEXTURE}__{base_name}"
        projection_tex_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_TEXTURE)
        projection_tex_node.image = proj_img
        projection_tex_node.location = (og_x, og_y - 400)
        
        # Connect UV to projection texture
        links.new(projection_uv_node.outputs['UV'], projection_tex_node.inputs['Vector'])
        
        # Create Projection Filter Mix node (between projection texture and final mix)
        try:
            projection_filter_node = nodes.new(type='ShaderNodeMix')
            projection_filter_node.data_type = 'RGBA'
            projection_filter_node.blend_type = 'MIX'
        except:
            # Fallback for older Blender versions
            projection_filter_node = nodes.new(type='ShaderNodeMixRGB')
            projection_filter_node.blend_type = 'MIX'
        
        projection_filter_node.name = f"{common.NODE_NAME_PROJECTION_FILTER}__{base_name}"
        projection_filter_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_FILTER)
        projection_filter_node.location = (og_x + 200, og_y - 400)
        
        # Apply filter settings from tex_node_data if available
        if tex_node_data:
            filter_type = tex_node_data.projection_filter_type
            filter_color = tex_node_data.projection_filter_color
            
            # Set blend type
            if filter_type != 'NONE':
                projection_filter_node.blend_type = filter_type
            
            # Set filter factor and color
            if projection_filter_node.type == 'MIX':
                # For new Mix node
                projection_filter_node.inputs[0].default_value = 0.0 if filter_type == 'NONE' else 1.0  # Factor
                projection_filter_node.inputs[7].default_value = filter_color  # Color B (filter color)
            else:
                # For legacy MixRGB node
                projection_filter_node.inputs['Fac'].default_value = 0.0 if filter_type == 'NONE' else 1.0
                projection_filter_node.inputs['Color2'].default_value = filter_color
        else:
            # Set default filter to bypass (factor = 0) if no tex_node_data
            if projection_filter_node.type == 'MIX':
                projection_filter_node.inputs[0].default_value = 0.0  # Factor
                projection_filter_node.inputs[7].default_value = (1.0, 1.0, 1.0, 1.0)  # Color B (filter color)
            else:
                projection_filter_node.inputs['Fac'].default_value = 0.0
                projection_filter_node.inputs['Color2'].default_value = (1.0, 1.0, 1.0, 1.0)
        
        # Connect projection texture to filter node
        if projection_filter_node.type == 'MIX':
            links.new(projection_tex_node.outputs['Color'], projection_filter_node.inputs[6])  # A
        else:
            links.new(projection_tex_node.outputs['Color'], projection_filter_node.inputs['Color1'])
        
        # Get filter output
        projection_filter_output = projection_filter_node.outputs[2] if projection_filter_node.type == 'MIX' else projection_filter_node.outputs['Color']
        
        # Create Vertex Color node for visibility mask
        vertex_color_node = nodes.new(type='ShaderNodeVertexColor')
        vertex_color_node.name = f"{common.NODE_NAME_PROJECTION_VISIBILITY}__{base_name}"
        vertex_color_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_VISIBILITY)
        vertex_color_node.layer_name = common.PROJECTION_VIS_VCOL_NAME
        vertex_color_node.location = (og_x + 200, og_y - 600)
        
        # Create Math Add node for mix offset
        math_add_node = nodes.new(type='ShaderNodeMath')
        math_add_node.operation = 'ADD'
        math_add_node.name = f"{common.NODE_NAME_PROJECTION_VISIBILITY_ADD}__{base_name}"
        math_add_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_VISIBILITY_ADD)
        math_add_node.location = (og_x + 350, og_y - 600)
        math_add_node.inputs[1].default_value = 0.0  # Default offset
        
        # Create Math Multiply node to combine visibility with projection alpha
        math_multiply_node = nodes.new(type='ShaderNodeMath')
        math_multiply_node.operation = 'MULTIPLY'
        math_multiply_node.name = f"{common.NODE_NAME_PROJECTION_ALPHA_MULTIPLY}__{base_name}"
        math_multiply_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_ALPHA_MULTIPLY)
        math_multiply_node.location = (og_x + 500, og_y - 600)
        
        # Create Mix node
        try:
            mix_node = nodes.new(type='ShaderNodeMix')
            mix_node.data_type = 'RGBA'
            mix_node.blend_type = 'MIX'
        except:
            # Fallback for older Blender versions
            mix_node = nodes.new(type='ShaderNodeMixRGB')
            mix_node.blend_type = 'MIX'
        
        mix_node.name = f"{common.NODE_NAME_PROJECTION_MIX}__{base_name}"
        mix_node.label = common.node_name_to_label(common.NODE_NAME_PROJECTION_MIX)
        mix_node.location = (og_x + 400, og_y - 200)
        
        # Connect vertex color to math add node
        links.new(vertex_color_node.outputs['Color'], math_add_node.inputs[0])
        
        # Connect math add output to math multiply node
        links.new(math_add_node.outputs['Value'], math_multiply_node.inputs[0])
        
        # Connect projection texture alpha to math multiply node
        links.new(projection_tex_node.outputs['Alpha'], math_multiply_node.inputs[1])
        
        # Connect math multiply output to mix node factor
        if mix_node.type == 'MIX':
            links.new(math_multiply_node.outputs['Value'], mix_node.inputs[0])  # Factor
        else:
            links.new(math_multiply_node.outputs['Value'], mix_node.inputs['Fac'])
        
        # Connect textures to Mix node
        if mix_node.type == 'MIX':
            links.new(og_img_node.outputs['Color'], mix_node.inputs[6])  # A
            links.new(projection_filter_output, mix_node.inputs[7])  # B (filtered projection)
            mix_output = mix_node.outputs[2]  # Result
        else:
            links.new(og_img_node.outputs['Color'], mix_node.inputs['Color1'])
            links.new(projection_filter_output, mix_node.inputs['Color2'])  # Filtered projection
            mix_output = mix_node.outputs['Color']
        
        # Handle alpha output if the original texture node's alpha is connected
        og_alpha_dest_socket = None
        if og_img_node.outputs['Alpha'].links:
            # Store the original alpha destination socket
            og_alpha_dest_socket = og_img_node.outputs['Alpha'].links[0].to_socket
            
            # Create Math Add node to combine original alpha with projection alpha multiply output
            alpha_combine_node = nodes.new(type='ShaderNodeMath')
            alpha_combine_node.operation = 'ADD'
            alpha_combine_node.name = f"{common.NODE_NAME_PROJECTION_ALPHA_MULTIPLY}_Add__{base_name}"
            alpha_combine_node.label = "Alpha Combine"
            alpha_combine_node.location = (og_x + 650, og_y - 600)
            
            # Connect original alpha to first input
            links.new(og_img_node.outputs['Alpha'], alpha_combine_node.inputs[0])
            
            # Connect projection alpha multiply output to second input
            links.new(math_multiply_node.outputs['Value'], alpha_combine_node.inputs[1])
            
            # Connect the combined alpha to the original destination
            links.new(alpha_combine_node.outputs['Value'], og_alpha_dest_socket)
        
        # Add alpha support if destination socket is a material output shader input
        if dest_socket and dest_socket.name in ['Surface', 'BSDF']:
            # Create Math Add node to combine original and projection texture alphas
            alpha_add_node = nodes.new(type='ShaderNodeMath')
            alpha_add_node.operation = 'ADD'
            alpha_add_node.name = "PROJECTION_Alpha_Add"
            alpha_add_node.label = "Alpha Add"
            alpha_add_node.location = (og_x + 600, og_y - 400)
            
            # Connect alpha outputs to the add node
            links.new(og_img_node.outputs['Alpha'], alpha_add_node.inputs[0])
            links.new(projection_tex_node.outputs['Alpha'], alpha_add_node.inputs[1])
            
            # Create Transparent BSDF node
            transparent_bsdf = nodes.new(type='ShaderNodeBsdfTransparent')
            transparent_bsdf.name = "PROJECTION_Transparent_BSDF"
            transparent_bsdf.label = "Transparent BSDF"
            transparent_bsdf.location = (og_x + 600, og_y + 100)
            
            # Create Mix Shader node to blend between transparent and the color mix
            mix_shader = nodes.new(type='ShaderNodeMixShader')
            mix_shader.name = "PROJECTION_Mix_Shader"
            mix_shader.label = "Alpha Mix Shader"
            mix_shader.location = (og_x + 800, og_y)
            
            # Connect alpha add output to mix shader factor
            links.new(alpha_add_node.outputs['Value'], mix_shader.inputs['Fac'])
            
            # Connect transparent BSDF to first shader input
            links.new(transparent_bsdf.outputs['BSDF'], mix_shader.inputs[1])
            # dest_socket expects a shader, so we need to convert our color mix to a shader first
            # Create an Emission shader to convert color to shader
            emission_shader = nodes.new(type='ShaderNodeEmission')
            emission_shader.name = "PROJECTION_Emission"
            emission_shader.label = "Color to Shader"
            emission_shader.location = (og_x + 600, og_y - 100)
            
            # Connect color mix output to emission
            links.new(mix_output, emission_shader.inputs['Color'])
            
            # Connect emission to second shader input of mix shader
            links.new(emission_shader.outputs['Emission'], mix_shader.inputs[2])
            
            # Connect mix shader output to destination
            links.new(mix_shader.outputs['Shader'], dest_socket)
        elif dest_socket:
            # Connect the color mix output directly
            links.new(mix_output, dest_socket)

        # Set up driver for mix offset
        if math_add_node:
            visibility_socket = math_add_node.inputs[1]
            # Get scene from context
            try:
                scene = context.scene
                visibility_socket.default_value = scene.cam_proj_paint.mix_offset
                try:
                    visibility_socket.driver_remove('default_value')
                except (TypeError, ValueError):
                    pass
                fcurve = visibility_socket.driver_add('default_value')
                fcurve.driver.type = 'SCRIPTED'
                fcurve.driver.expression = "mix_offset"
                var = fcurve.driver.variables.new()
                var.name = "mix_offset"
                target = var.targets[0]
                target.id_type = 'SCENE'
                target.id = scene
                target.data_path = "cam_proj_paint.mix_offset"
            except Exception as e:
                print(f"Warning: Failed to set up mix offset driver: {str(e)}")
        
        # Return the projection texture node name and filter node name for storage
        return projection_tex_node.name, projection_filter_node.name
        
    except Exception as e:
        print(f"Error setting up projection mix: {str(e)}")
        import traceback
        traceback.print_exc()
        return None, None

def restore_original_materials(obj):
    """Replace all preview materials with original materials. Also deletes bake materials and associated bake target textures.
    """
    for mat_data in obj.cam_proj_paint.material_data:
        try:
            original_mat = bpy.data.materials.get(mat_data.original_material_name)
            preview_mat = bpy.data.materials.get(mat_data.preview_material_name)

            # Replace preview material with original
            if preview_mat and original_mat:
                for slot in obj.material_slots:
                    if slot.material == preview_mat:
                        slot.material = original_mat
                # Remove preview material from datablocks
                common.remove_material(preview_mat)
            
            # Clean up per-texture-node bake materials and targets
            for tex_node_data in mat_data.texture_nodes:
                # Remove bake material for this texture node
                if tex_node_data.bake_material_name:
                    bake_mat = bpy.data.materials.get(tex_node_data.bake_material_name)
                    if bake_mat:
                        # Remove bake material from datablocks
                        common.remove_material(bake_mat)
                
                # Remove bake target image for this texture node
                if tex_node_data.bake_target_texture:
                    common.remove_image(tex_node_data.bake_target_texture)
            
            # Replace the slot with the original material
            print(f"Restoring material from {preview_mat.name} to {original_mat.name} ({obj.name} - Slot: {mat_data.material_index})")
            if obj.data.materials[mat_data.material_index] != original_mat:
                obj.data.materials[mat_data.material_index] = original_mat
        
        except Exception:
            print(f"Warning: Failed to restore original material for {obj.name} - {mat_data.original_material_name}")
            continue

# =====================================================================
# BAKE FUNCTIONS
# =====================================================================

def bake_projection_core_psd(context, enabled_objects, render_backend):
    """
    Bake loop for handling multiple bakes per material.
    """
    import traceback

    # Store original selection and active object
    selection_state = common.store_selection_state(context)

    # Make sure we're in object mode
    common.ensure_object_mode(context)

    baked_count = 0
    total_texture_nodes_baked = 0

    print("\n" + "="*50)
    print("Baking projection (PSD Multi-Texture Mode)")
    print("="*50)
    
    for obj in enabled_objects:
        try:
            # Skip objects with preview_only enabled
            if obj.cam_proj_paint.preview_only:
                print(f"\n{obj.name}: Skipped (Preview Only)")
                continue
            
            print(f"\n{obj.name}:")
            obj_texture_nodes_baked = 0

            # Step 1: Duplicate object for baking
            temp_obj = uv_bake_eevee.duplicate_object_for_baking(obj)
            if not temp_obj:
                print(f"  {obj.name}: ✗ Failed to duplicate for baking")
                continue
            
            # Step 2: Separate by materials (creates one object per material)
            print(f"  Separating '{temp_obj.name}' by materials...")
            separated_objects = uv_bake_eevee.separate_object_by_materials(temp_obj)
            
            if not separated_objects:
                print(f"  {obj.name}: ✗ No separated objects created")
                common.remove_object(temp_obj)
                continue
            
            # Map original material names to their indices
            original_materials = [slot.material for slot in obj.material_slots]
            og_mat_indexes = {m.name: i for i, m in enumerate(original_materials) if m}
            
            # Step 3: For each separated object (single material each)
            for sep_obj in separated_objects:
                try:
                    # Get the material from the separated object
                    if not sep_obj.data.materials or not sep_obj.data.materials[0]:
                        print(f"    Separated object '{sep_obj.name}': ✗ No material")
                        continue
                    
                    sep_mat = sep_obj.data.materials[0]
                    mat_index = og_mat_indexes.get(sep_mat.name)
                    
                    if mat_index is None:
                        print(f"    Separated object '{sep_obj.name}': ✗ Material '{sep_mat.name}' not in original")
                        continue
                    
                    # Find the corresponding material data
                    mat_data = next((md for md in obj.cam_proj_paint.material_data if md.material_index == mat_index), None)
                    if not mat_data:
                        print(f"    Material '{sep_mat.name}': ✗ No material data found")
                        continue
                    
                    print(f"\n  Material '{sep_mat.name}' (slot {mat_index}):")
                    
                    # Step 4: For each texture node in this material
                    for tex_node_data in mat_data.texture_nodes:
                        if not tex_node_data.bake_material_name:
                            print(f"    Node '{tex_node_data.node_name}': ✗ No bake material assigned")
                            continue
                        
                        bake_mat = bpy.data.materials.get(tex_node_data.bake_material_name)
                        if not bake_mat:
                            print(f"    Node '{tex_node_data.node_name}': ✗ Bake material not found")
                            continue
                        
                        if not tex_node_data.bake_target_texture:
                            print(f"    Node '{tex_node_data.node_name}': ✗ No bake target texture")
                            continue
                        
                        # Step 5: Assign the bake material to the separated object
                        # (Separated object has only one material slot)
                        original_sep_mat = sep_obj.data.materials[0]
                        sep_obj.data.materials[0] = bake_mat
                        
                        # Step 6: Pass to backend which should render this single-material object
                        try:
                            rendered_img = render_backend(sep_obj, context, tex_node_data)
                        except Exception as e:
                            print(f"    Node '{tex_node_data.node_name}': ✗ Backend error - {str(e)}")
                            traceback.print_exc()
                            # Restore material
                            sep_obj.data.materials[0] = original_sep_mat
                            continue
                        
                        if not rendered_img:
                            print(f"    Node '{tex_node_data.node_name}': ✗ No rendered image")
                            # Restore material
                            sep_obj.data.materials[0] = original_sep_mat
                            continue
                        
                        # Step 7: Copy result to bake target
                        target_img = tex_node_data.bake_target_texture
                        
                        # Resize if needed
                        if target_img.size[0] != rendered_img.size[0] or target_img.size[1] != rendered_img.size[1]:
                            common.resize_image(target_img, rendered_img.size[0], rendered_img.size[1])
                        
                        print(f"    Node '{tex_node_data.node_name}': Copying baked pixels to '{target_img.name}'")
                        try:
                            if common.copy_image_pixels(rendered_img, target_img):
                                target_img.update()
                                target_img.use_fake_user = True
                                layer_info = f" (PSD: {tex_node_data.psd_layer_name})" if tex_node_data.psd_layer_name else ""
                                print(f"    Node '{tex_node_data.node_name}': ✓ Baked successfully{layer_info}")
                                obj_texture_nodes_baked += 1
                                total_texture_nodes_baked += 1
                            else:
                                print(f"    Node '{tex_node_data.node_name}': ✗ Failed to copy pixels")
                        except Exception as e:
                            print(f"    Node '{tex_node_data.node_name}': ✗ Failed to copy pixels - {str(e)}")
                        
                        # Clean up rendered image
                        common.remove_image(rendered_img)
                        
                        # Restore material for next texture node
                        sep_obj.data.materials[0] = original_sep_mat
                
                except Exception as e:
                    print(f"    Separated object error: {str(e)}")
                    traceback.print_exc()
            
            # Cleanup all separated objects
            for sep_obj in separated_objects:
                common.remove_object(sep_obj)

            if obj_texture_nodes_baked > 0:
                print(f"\n  {obj.name}: ✓ Baked {obj_texture_nodes_baked} texture node(s)")
                baked_count += 1
            else:
                print(f"\n  {obj.name}: ✗ No texture nodes baked")

        except Exception as e:
            print(f"  {obj.name}: ✗ Error - {str(e)}")
            traceback.print_exc()

    print("="*50)
    print(f"Baked {total_texture_nodes_baked} texture node(s) on {baked_count}/{len(enabled_objects)} objects")
    print("="*50 + "\n")

    # Cleanup temporary collection if empty (UV bake temp)
    temp_collection = bpy.data.collections.get(common.UV_BAKE_TEMP_COLLECTION)
    if temp_collection and len(temp_collection.objects) == 0:
        try:
            common.remove_collection(temp_collection)
        except:
            pass

    # Restore original selection and active object
    common.restore_selection_state(context, selection_state)

    return baked_count, total_texture_nodes_baked

def eevee_backend_psd(sep_obj, context, tex_node_data):
    """
    EEVEE backend to bake a single-material object.
    
    Args:
        sep_obj: Already separated object with single material (has bake material assigned)
        context: Blender context
        tex_node_data: TextureNodeData to get resolution from bake_target_texture
        
    Returns:
        Single rendered Image or None
    """
    try:
        # Get UV map name - use the original UV map connected to this texture node if available
        uv_map_name = None
        if tex_node_data.original_uv_map and sep_obj.data.uv_layers.get(tex_node_data.original_uv_map):
            uv_map_name = tex_node_data.original_uv_map
        else:
            # Fallback to the first available UV layer
            uv_map_name = sep_obj.data.uv_layers[0].name if sep_obj.data.uv_layers else "UVMap"
        
        # Unfold UVs for separated object using the correct UV map
        try:
            uv_bake_eevee.unfold_mesh_to_uv_space(sep_obj, uv_map_name)
        except Exception as e:
            print(f"      UV unfold failed: {e}")
            pass

        # Get resolution from texture node's bake target
        resolution_x = tex_node_data.bake_target_texture.size[0] if tex_node_data.bake_target_texture else 2048
        resolution_y = tex_node_data.bake_target_texture.size[1] if tex_node_data.bake_target_texture else 2048

        # Setup camera for UV baking
        uv_bake_camera = uv_bake_eevee.setup_uv_bake_camera(sep_obj, resolution_x, resolution_y)

        # Configure render settings
        scene = context.scene
        original_settings = uv_bake_eevee.configure_render_settings(scene, resolution_x, resolution_y, use_transparent=True)
        
        # Set the active camera
        scene.camera = uv_bake_camera
        
        # Create temporary image for render result
        temp_image = common.create_image(
            name=f"{common.TEMP_BAKE_IMAGE_PREFIX}{sep_obj.name}",
            width=resolution_x,
            height=resolution_y,
            alpha=True
        )
        
        if not temp_image:
            original_settings.restore(scene)
            return None
        
        # Render to image
        success = uv_bake_eevee.render_to_image(scene, uv_bake_camera, temp_image, render_object=sep_obj, margin_pixels=4)
        
        # Restore settings
        original_settings.restore(scene)
        
        if success:
            return temp_image
        else:
            common.remove_image(temp_image)
            return None

    except Exception as e:
        print(f"      EEVEE PSD backend failed: {str(e)}")
        return None

def cycles_backend_psd(sep_obj, context, tex_node_data):
    """
    Cycles backend to bake a single-material object.
    
    Args:
        sep_obj: Already separated object with single material (has bake material assigned)
        context: Blender context
        tex_node_data: TextureNodeData to get resolution from bake_target_texture
        
    Returns:
        Single rendered Image or None
    """
    try:
        bake_mat = sep_obj.data.materials[0] if sep_obj.data.materials else None
        if not bake_mat or not bake_mat.use_nodes:
            return None

        nodes = bake_mat.node_tree.nodes
        bake_target_node = nodes.get(common.NODE_NAME_BAKE_TARGET)
        if not bake_target_node:
            return None

        # Create a temporary image for this bake
        width = tex_node_data.bake_target_texture.size[0] if tex_node_data.bake_target_texture else 2048
        height = tex_node_data.bake_target_texture.size[1] if tex_node_data.bake_target_texture else 2048
        tmp_img = common.create_image(f"TMP_BAKE_{sep_obj.name}", width, height, alpha=True)

        if not tmp_img:
            return None

        # Assign temp image to the bake target node
        bake_target_node.image = tmp_img

        # Select separated object and make active
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except:
            pass
        sep_obj.select_set(True)
        context.view_layer.objects.active = sep_obj

        # Ensure the bake target node is active
        for n in nodes:
            n.select = False
        bake_target_node.select = True
        nodes.active = bake_target_node

        # Perform Cycles bake
        try:
            # Ensure transparent film for Cycles so bakes include alpha
            scene = context.scene
            prev_film_transparent = getattr(scene.render, 'film_transparent', False)
            try:
                scene.render.film_transparent = True
                bpy.ops.object.bake(
                    type='DIFFUSE',
                    pass_filter={'COLOR'},
                    use_selected_to_active=False,
                    margin=4,
                    use_clear=True
                )
            finally:
                # Restore previous film transparency setting
                try:
                    scene.render.film_transparent = prev_film_transparent
                except:
                    pass
        except Exception as e:
            # If bake failed, remove tmp image
            common.remove_image(tmp_img)
            return None

        return tmp_img

    except Exception as e:
        print(f"      Cycles PSD backend failed: {str(e)}")
        return None

# =====================================================================
# HELPER FUNCTIONS
# =====================================================================

def has_bake_materials_ready(enabled_objects):
    """
    Check if any enabled object has bake materials set up.
    
    Args:
        enabled_objects: List of enabled objects to check
        
    Returns:
        bool: True if bake materials are found, False otherwise
    """
    return any(
        any(any(tn.bake_material_name for tn in mat_data.texture_nodes) for mat_data in obj.cam_proj_paint.material_data)
        for obj in enabled_objects
    )

def has_bake_targets_ready(enabled_objects):
    """
    Check if any enabled object has bake target textures ready (i.e., baking is complete).
    
    Args:
        enabled_objects: List of enabled objects to check
        
    Returns:
        bool: True if bake target textures are found, False otherwise
    """
    return any(
        any(any(tn.bake_target_texture for tn in mat_data.texture_nodes) for mat_data in obj.cam_proj_paint.material_data)
        for obj in enabled_objects
    )

def object_has_preview_materials(obj):
    """
    Check if the given object has any preview materials assigned.
    
    Args:
        obj: Blender object to check
    Returns:
        bool: True if any preview materials are assigned, False otherwise
    """
    return any(
        bpy.data.materials.get(mat_data.preview_material_name) is not None
        for mat_data in obj.cam_proj_paint.material_data
    )

def is_image_a_psd_layer(image, psd_file_path):
    """
    Check if a given Blender image corresponds to a layer in the PSD file.
    
    Args:
        image: Blender Image datablock to check
        psd_file_path: Path to the PSD file
    
    Returns:
        bool: True if the image corresponds to a layer in the PSD file, False otherwise
    """
    if not image or not psd_file_path:
        return False
    
    if not os.path.exists(psd_file_path):
        return False
    
    # Check if image name starts with PSD_ prefix (our naming convention)
    if not image.name.startswith("PSD_"):
        return False
    
    # Extract the layer name from the image name (remove "PSD_" prefix)
    layer_name = image.name[4:]  # Remove "PSD_" prefix
    
    # Get all layers from PSD file
    try:
        psd_layers = psd_handler.get_psd_layer_list(psd_file_path)
        if not psd_layers:
            return False
        
        # Check if this layer name exists in the PSD file
        for psd_layer_name, is_group in psd_layers:
            if not is_group and psd_layer_name == layer_name:
                return True
        
        return False
    except Exception as e:
        print(f"Error checking if image '{image.name}' is a PSD layer: {e}")
        return False

def reload_texture_node(tex_node_data, context):
    """
    Reload PSD layer for a single texture node.
    
    Args:
        tex_node_data: CAMPROJPAINT_TextureNodeData instance
        context: Blender context
    """
    settings = context.scene.cam_proj_paint
    psd_file_path = settings.projection_psd_file
    
    if not psd_file_path or not os.path.exists(psd_file_path):
        print(f"PSD file not found: {psd_file_path}")
        return
    
    if not tex_node_data.projection_texture_node_name:
        print(f"No projection texture node name stored for '{tex_node_data.node_name}'")
        return
    
    # If no PSD layer assigned, set projection image to None
    if not tex_node_data.psd_layer_name or tex_node_data.psd_layer_name == 'NONE':
        layer_image = None
    else:
        # Extract the PSD layer
        try:
            layer_image = psd_handler.extract_single_layer(
                psd_file_path,
                tex_node_data.psd_layer_name,
                as_blender_image=True,
                image_name=f"PSD_{tex_node_data.psd_layer_name}"
            )
            
            if not layer_image:
                print(f"Failed to extract layer '{tex_node_data.psd_layer_name}'")
                return
        except Exception as e:
            print(f"Error extracting layer '{tex_node_data.psd_layer_name}': {str(e)}")
            return
    
    try:
        
        # Find the material data that contains this texture node
        obj = context.active_object
        if not obj or not hasattr(obj, 'cam_proj_paint'):
            print(f"No active object with cam_proj_paint data")
            return
        
        mat_data = None
        for md in obj.cam_proj_paint.material_data:
            for tn in md.texture_nodes:
                if tn == tex_node_data:
                    mat_data = md
                    break
            if mat_data:
                break
        
        if not mat_data:
            print(f"Could not find material data for texture node '{tex_node_data.node_name}'")
            return
        
        # Update bake material if it exists
        updated_bake = False
        bake_mat = bpy.data.materials.get(tex_node_data.bake_material_name)
        if bake_mat and bake_mat.use_nodes:
            nodes = bake_mat.node_tree.nodes
            projection_tex_node = nodes.get(tex_node_data.projection_texture_node_name)
            
            if projection_tex_node:
                projection_tex_node.image = layer_image
                updated_bake = True
            else:
                print(f"Projection texture node '{tex_node_data.projection_texture_node_name}' not found in bake material")
        
        # Update preview material if it exists
        updated_preview = False
        preview_mat = bpy.data.materials.get(mat_data.preview_material_name)
        if preview_mat and preview_mat.use_nodes:
            nodes = preview_mat.node_tree.nodes
            projection_tex_node = nodes.get(tex_node_data.projection_texture_node_name)
            
            if projection_tex_node:
                # Delete old layer image if it does not correspond to any layer in the PSD file anymore
                old_image = projection_tex_node.image
                if old_image and not is_image_a_psd_layer(old_image, psd_file_path):
                    print(f"  Removing obsolete PSD layer image: '{old_image.name}'")
                    common.remove_image(old_image)
                
                projection_tex_node.image = layer_image
                updated_preview = True
            else:
                print(f"Projection texture node '{tex_node_data.projection_texture_node_name}' not found in preview material")
        
        if updated_bake or updated_preview:
            status = []
            if updated_bake:
                status.append("bake")
            if updated_preview:
                status.append("preview")
            
            if layer_image:
                print(f"✓ Reloaded '{tex_node_data.node_name}' with layer '{tex_node_data.psd_layer_name}' ({', '.join(status)})")
            else:
                print(f"✓ Cleared projection image for '{tex_node_data.node_name}' ({', '.join(status)})")
        else:
            print(f"✗ No materials updated for texture node '{tex_node_data.node_name}'")
            
    except Exception as e:
        print(f"Error reloading texture node '{tex_node_data.node_name}': {str(e)}")
        import traceback
        traceback.print_exc()

# =====================================================================
# PROPERTY GROUPS
# =====================================================================

class CAMPROJPAINT_TextureNodeData(PropertyGroup):
    """Data for each image texture node in a material"""
    node_name: StringProperty(
        name="Node Name",
        description="Name of the image texture node in the material",
        default=""
    )
    
    original_texture: PointerProperty(
        name="Original Texture",
        description="Original texture for this node",
        type=bpy.types.Image
    )
    
    original_uv_map: StringProperty(
        name="Original UV Map",
        description="UV map associated with this texture node",
        default=""
    )
    
    psd_layer_name: StringProperty(
        name="PSD Layer",
        description="Name of the PSD layer to use for this texture node",
        default=""
    )
    
    psd_layer_enum: EnumProperty(
        name="PSD Layer",
        description="Select PSD layer for this texture node",
        items=get_psd_layer_items,
        update=on_psd_layer_enum_update
    )
    
    projection_texture_node_name: StringProperty(
        name="Projection Texture Node",
        description="Name of the projection texture node for this texture node",
        default=""
    )
    
    bake_target_texture: PointerProperty(
        name="Bake Target Image",
        description="Temporary bake target image for this texture node",
        type=bpy.types.Image
    )
    
    # Bake material info
    bake_material_name: StringProperty(
        name="Bake Material",
        description="Name of the dedicated bake material for this texture node",
        default=""
    )
    
    # Projection filter settings
    projection_filter_type: EnumProperty(
        name="Filter Type",
        description="Blend mode for projection filter",
        items=[
            ('NONE', "None", "No filter applied"),
            ('MIX', "Mix", "Mix filter color"),
            ('MULTIPLY', "Multiply", "Multiply with filter color"),
            ('DIVIDE', "Divide", "Divide by filter color"),
            ('ADD', "Add", "Add filter color"),
            ('SUBTRACT', "Subtract", "Subtract filter color"),
            ('SCREEN', "Screen", "Screen blend"),
            ('OVERLAY', "Overlay", "Overlay blend"),
            ('COLOR', "Color", "Color blend"),
            ('HUE', "Hue", "Hue blend"),
            ('SATURATION', "Saturation", "Saturation blend"),
            ('VALUE', "Value", "Value blend"),
        ],
        default='NONE',
        update=on_projection_filter_update
    )
    
    projection_filter_color: FloatVectorProperty(
        name="Filter Color",
        description="Color for projection filter",
        size=4,
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        subtype='COLOR',
        update=on_projection_filter_update
    )
    
    projection_filter_mix_node_name: StringProperty(
        name="Projection Filter Mix Node",
        description="Name of the projection filter mix node",
        default=""
    )

class CAMPROJPAINT_MaterialData(PropertyGroup):
    """Data for each material slot"""
    material_index: IntProperty(
        name="Material Index",
        description="Index of this material slot",
        default=0
    )
    
    original_material_name: StringProperty(
        name="Original Material",
        description="Name of the original material",
        default=""
    )
    
    preview_material_name: StringProperty(
        name="Preview Material",
        description="Name of the preview material (duplicate of original with projection mix)",
        default=""
    )
    
    texture_nodes: CollectionProperty(
        type=CAMPROJPAINT_TextureNodeData,
        name="Texture Nodes",
        description="Collection of texture nodes in this material (for multi-layer PSD workflow)"
    )

class CAMPROJPAINT_ObjectSettings(PropertyGroup):
    """Per-object settings for projection paint"""
    enabled: BoolProperty(
        name="Enabled",
        description="Enable this object for projection paint workflows",
        default=False,
        update=on_enabled_update
    )

    original_uv: StringProperty(
        name="Original UV",
        description="Name of the original UV map before projection",
        default=""
    )

    projection_mode: EnumProperty(
        name="Projection Mode",
        description="Control which surfaces receive projection",
        items=[
            ('VISIBLE', "Visible Only", "Project only on camera-facing surfaces"),
            ('ALL', "All Surfaces", "Project on all surfaces including backfaces"),
            ('PRESERVE', "Preserve", "Preserve existing visibility mask (skip update)"),
        ],
        default='VISIBLE'
    )
    
    preserve_uv: BoolProperty(
        name="Preserve UV",
        description="Preserve existing projection UVs (skip UV update during preview/bake setup)",
        default=False
    )
    
    preview_only: BoolProperty(
        name="Preview Only",
        description="Enable preview for this object but skip it during baking operations",
        default=False
    )
    
    projection_frame: IntProperty(
        name="Projection Frame",
        description="Frame number when projection UVs and visibility were created",
        default=-1
    )
    
    use_projection_frame: BoolProperty(
        name="Use Projection Frame",
        description="Use the stored projection frame when updating preview/bake instead of current frame",
        default=False
    )

    material_data: CollectionProperty(
        type=CAMPROJPAINT_MaterialData
    )

class CAMPROJPAINT_SceneSettings(PropertyGroup):
    """Scene-level settings for Camera Projection Paint"""
    projection_psd_file: StringProperty(
        name="Projection PSD File",
        description="PSD file containing multiple layers for multi-texture projection",
        subtype='FILE_PATH',
        default=""
    )
    
    bake_method: EnumProperty(
        name="Bake Method",
        description="Choose baking method",
        items=[
            ('EEVEE', "EEVEE (Fast)", "Use EEVEE rendering for fast baking"),
            ('CYCLES', "Cycles", "Use Cycles for accurate baking (slower)"),
        ],
        default='EEVEE'
    )
    
    ignore_material_prefixes: StringProperty(
        name="Ignore Material Prefixes",
        description="Comma-separated list of material name prefixes to ignore (e.g. 'TEMP_,IGNORE_,SKIP_')",
        default="OL_"
    )

    mix_offset: FloatProperty(
        name="Mix Offset",
        description="Additive offset applied to the projection visibility mask before mixing",
        default=0.0,
        min=-1.0,
        max=1.0
    )
    
    auto_map_on_reload: BoolProperty(
        name="Auto-Map on Reload",
        description="Automatically map PSD layers to texture nodes when reloading",
        default=False
    )
    
    auto_reload_enabled: BoolProperty(
        name="Auto-Reload Enabled",
        description="Automatically start watchdog on file load to monitor PSD file changes",
        default=False
    )

# =====================================================================
# OPERATORS
# =====================================================================

class CAMPROJPAINT_OT_install_dependencies(Operator):
    """Install required Python packages (watchdog, psd-tools)"""
    bl_idname = "camprojpaint.install_dependencies"
    bl_label = "Install Dependencies"
    bl_description = "Install required Python packages: watchdog (for auto-reload) and psd-tools (for PSD layer extraction)"
    
    def execute(self, context):
        import sys
        import subprocess
        
        # Get Python executable
        python_exe = sys.executable
        
        self.report({'INFO'}, "Installing dependencies... This may take a minute.")
        print("\n" + "="*50)
        print("Installing required packages...")
        print("="*50)
        
        packages = ['watchdog', 'psd-tools']
        failed_packages = []
        
        for package in packages:
            try:
                print(f"\nInstalling {package}...")
                result = subprocess.run(
                    [python_exe, "-m", "pip", "install", package],
                    capture_output=True,
                    text=True,
                    timeout=120  # 2 minute timeout per package
                )
                
                if result.returncode == 0:
                    print(f"✓ {package} installed successfully")
                else:
                    print(f"✗ Failed to install {package}")
                    print(f"Error: {result.stderr}")
                    failed_packages.append(package)
                    
            except subprocess.TimeoutExpired:
                print(f"✗ Timeout installing {package}")
                failed_packages.append(package)
            except Exception as e:
                print(f"✗ Error installing {package}: {str(e)}")
                failed_packages.append(package)
        
        print("="*50)
        
        if failed_packages:
            self.report({'ERROR'}, f"Failed to install: {', '.join(failed_packages)}. Check console for details.")
            return {'CANCELLED'}
        else:
            self.report({'INFO'}, "Dependencies installed successfully! Please restart Blender.")
            print("\n⚠ IMPORTANT: Please restart Blender for changes to take effect.\n")
            return {'FINISHED'}

class CAMPROJPAINT_OT_toggle_auto_reload(Operator):
    """Toggle automatic file watching for PSD file"""
    bl_idname = "camprojpaint.toggle_auto_reload"
    bl_label = "Toggle Auto-Reload"
    bl_description = "Start/stop watching PSD file for changes"
    
    def execute(self, context):
        scene = context.scene
        
        if not psd_watcher.WATCHDOG_AVAILABLE:
            self.report({'ERROR'}, "Watchdog library not installed. Install with pip.")
            return {'CANCELLED'}
        
        if not psd_watcher.is_watching():
            # Start watching
            if psd_watcher.start_watching_psd_file(scene):
                scene.cam_proj_paint.auto_reload_enabled = True
                self.report({'INFO'}, "Auto-reload enabled - watching PSD file for changes")
            else:
                psd_watcher.stop_watching()
                scene.cam_proj_paint.auto_reload_enabled = False
                self.report({'ERROR'}, "Failed to start PSD file watching")
                return {'CANCELLED'}
        else:
            # Stop watching
            psd_watcher.stop_watching()
            scene.cam_proj_paint.auto_reload_enabled = False
            self.report({'INFO'}, "Auto-reload disabled")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_enable_visible_objects(Operator):
    """Enable all objects visible from the active camera"""
    bl_idname = "camprojpaint.enable_visible_objects"
    bl_label = "Enable Visible Objects"
    bl_description = "Enable all mesh objects visible from the active camera for projection workflows"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Check if there's an active camera
        if not context.scene.camera:
            self.report({'ERROR'}, "No active camera in scene")
            return {'CANCELLED'}
        
        # Get visible objects from camera
        visible_objects = common.get_visible_objects_from_camera(context)
        
        if not visible_objects:
            self.report({'WARNING'}, "No mesh objects visible from camera")
            return {'CANCELLED'}
        
        enabled_count = 0
        
        for obj in visible_objects:
            if not hasattr(obj, 'cam_proj_paint'):
                continue
            
            if not obj.cam_proj_paint.enabled:
                obj.cam_proj_paint.enabled = True
                enabled_count += 1
        
        self.report({'INFO'}, f"Enabled {enabled_count} objects visible from camera")
        return {'FINISHED'}

class CAMPROJPAINT_OT_enable_selected_objects(Operator):
    """Enable selected objects for projection workflows"""
    bl_idname = "camprojpaint.enable_selected_objects"
    bl_label = "Enable Selected"
    bl_description = "Enable selected mesh objects for projection workflows"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        
        enabled_count = 0
        
        for obj in selected_meshes:
            if not hasattr(obj, 'cam_proj_paint'):
                continue
            
            if not obj.cam_proj_paint.enabled:
                obj.cam_proj_paint.enabled = True
                ensure_obj_material_data(obj, parse_ignore_prefixes(context.scene))
                enabled_count += 1
        
        self.report({'INFO'}, f"Enabled {enabled_count} selected objects")
        return {'FINISHED'}

class CAMPROJPAINT_OT_select_enabled_objects(Operator):
    """Select all enabled objects for projection workflows"""
    bl_idname = "camprojpaint.select_enabled_objects"
    bl_label = "Select Enabled Objects"
    bl_description = "Select all mesh objects enabled for projection workflows"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except:
            pass
        
        for obj in enabled_objects:
            obj.select_set(True)
        
        if enabled_objects:
            context.view_layer.objects.active = enabled_objects[0]
        
        self.report({'INFO'}, f"Selected {len(enabled_objects)} enabled objects")
        return {'FINISHED'}

class CAMPROJPAINT_OT_discover_texture_nodes(Operator):
    """Discover texture nodes for active object"""
    bl_idname = "camprojpaint.discover_texture_nodes"
    bl_label = "Discover Texture Nodes"
    bl_description = "Discover image texture nodes for active mesh object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        ensure_obj_material_data(obj, parse_ignore_prefixes(context.scene), rediscover_tex_nodes=True)
        return {'FINISHED'}

class CAMPROJPAINT_OT_disable_selected_objects(Operator):
    """Disable selected objects from projection workflows"""
    bl_idname = "camprojpaint.disable_selected_objects"
    bl_label = "Disable Selected"
    bl_description = "Disable selected mesh objects from projection workflows, restore original materials, and clean up stored data"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        
        disabled_count = 0
        
        print("\n" + "="*50)
        print("Disabling objects and restoring materials")
        print("="*50)
        
        for obj in selected_meshes:
            if not hasattr(obj, 'cam_proj_paint'):
                continue
            
            if obj.cam_proj_paint.enabled:
                restore_original_materials(obj)
                obj.cam_proj_paint.enabled = False
                disabled_count += 1
        
        print("="*50)
        print(f"Disabled {disabled_count} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Disabled {disabled_count} selected objects")
        return {'FINISHED'}

class CAMPROJPAINT_OT_auto_map_psd_layers(Operator):
    """Automatically map texture nodes to PSD layers by matching names"""
    bl_idname = "camprojpaint.auto_map_psd_layers"
    bl_label = "Auto-Map PSD Layers"
    bl_description = "Automatically assign PSD layers to texture nodes by matching image names"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        settings = scene.cam_proj_paint
        
        if not psd_handler.is_psd_available():
            self.report({'ERROR'}, "psd-tools not installed!")
            return {'CANCELLED'}
        
        if not settings.projection_psd_file:
            self.report({'ERROR'}, "No PSD file selected")
            return {'CANCELLED'}
        
        import os
        if not os.path.exists(settings.projection_psd_file):
            self.report({'ERROR'}, "PSD file not found")
            return {'CANCELLED'}
        
        enabled_objects = common.get_enabled_objects(context)
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        print("\n" + "="*50)
        print("Auto-Mapping PSD Layers")
        print("="*50)
        
        mapped_count, total_nodes = auto_map_psd_layers_to_textures(
            context,
            settings.projection_psd_file,
            enabled_objects,
            verbose=True
        )
        
        print("="*50)
        print(f"Auto-mapped {mapped_count}/{total_nodes} texture nodes")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Auto-mapped {mapped_count}/{total_nodes} texture nodes")
        return {'FINISHED'}

class CAMPROJPAINT_OT_set_project_visible(Operator):
    """Set selected enabled objects to project only on camera-facing surfaces"""
    bl_idname = "camprojpaint.set_project_visible"
    bl_label = "Project Visible Only"
    bl_description = "Project only on surfaces visible to camera for selected enabled objects (or all enabled if none selected)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Check if there's an active camera
        if not context.scene.camera:
            self.report({'ERROR'}, "No active camera in scene")
            return {'CANCELLED'}
        
        camera = context.scene.camera
        
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        # If nothing selected, use all enabled objects
        if not selected_enabled:
            selected_enabled = enabled_objects
        
        if not selected_enabled:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        updated_count = 0
        
        print("\n" + "="*50)
        print("Setting to project visible surfaces")
        print("="*50)
        
        for obj in selected_enabled:
            try:
                # Clear Mirror Motion accumulated motion before visibility calculation
                if hasattr(obj, 'mirror_motion') and obj.mirror_motion.enabled:
                    try:
                        import sys
                        mirror_motion_module = sys.modules.get('mirror_motion')
                        if mirror_motion_module:
                            clear_func = getattr(mirror_motion_module, 'clear_motion_and_history_object', None)
                            if clear_func:
                                clear_func(obj.mirror_motion, obj)
                                print(f"  {obj.name}: Cleared Mirror Motion accumulated motion")
                    except Exception as e:
                        print(f"  {obj.name}: Warning - Could not clear Mirror Motion: {e}")
                
                obj.cam_proj_paint.projection_mode = 'VISIBLE'

                if common.calculate_camera_visibility(obj, camera):
                    print(f"  {obj.name}: ✓ Set to camera-facing only")
                    updated_count += 1
                else:
                    print(f"  {obj.name}: ✗ Failed to update")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
        
        print("="*50)
        print(f"Updated {updated_count}/{len(selected_enabled)} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Set {updated_count} objects to project visible surfaces")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_set_project_all(Operator):
    """Set selected enabled objects to project on all surfaces"""
    bl_idname = "camprojpaint.set_project_all"
    bl_label = "Project All Surfaces"
    bl_description = "Project on all surfaces (including backfaces) for selected enabled objects (or all enabled if none selected)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Check if there's an active camera
        if not context.scene.camera:
            self.report({'ERROR'}, "No active camera in scene")
            return {'CANCELLED'}
        
        camera = context.scene.camera
        
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        # If nothing selected, use all enabled objects
        if not selected_enabled:
            selected_enabled = enabled_objects
        
        if not selected_enabled:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        updated_count = 0
        
        print("\n" + "="*50)
        print("Setting to project all surfaces")
        print("="*50)
        
        for obj in selected_enabled:
            try:
                # Clear Mirror Motion accumulated motion before visibility calculation
                if hasattr(obj, 'mirror_motion') and obj.mirror_motion.enabled:
                    try:
                        import sys
                        mirror_motion_module = sys.modules.get('mirror_motion')
                        if mirror_motion_module:
                            clear_func = getattr(mirror_motion_module, 'clear_motion_and_history_object', None)
                            if clear_func:
                                clear_func(obj.mirror_motion, obj)
                                print(f"  {obj.name}: Cleared Mirror Motion accumulated motion")
                    except Exception as e:
                        print(f"  {obj.name}: Warning - Could not clear Mirror Motion: {e}")
                
                obj.cam_proj_paint.projection_mode = 'ALL'

                if common.calculate_camera_visibility(obj, camera, fill=1.0):
                    print(f"  {obj.name}: ✓ Set to project all surfaces")
                    updated_count += 1
                else:
                    print(f"  {obj.name}: ✗ Failed to update")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
        
        print("="*50)
        print(f"Updated {updated_count}/{len(selected_enabled)} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Set {updated_count} objects to project all surfaces")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_set_project_preserve(Operator):
    """Set selected enabled objects to preserve visibility mask"""
    bl_idname = "camprojpaint.set_project_preserve"
    bl_label = "Preserve Mask"
    bl_description = "Preserve existing visibility mask for selected enabled objects (or all enabled if none selected)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        # If nothing selected, use all enabled objects
        if not selected_enabled:
            selected_enabled = enabled_objects
        
        if not selected_enabled:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        updated_count = 0
        
        print("\n" + "="*50)
        print("Setting to preserve visibility mask")
        print("="*50)
        
        for obj in selected_enabled:
            try:
                obj.cam_proj_paint.projection_mode = 'PRESERVE'
                print(f"  {obj.name}: ✓ Visibility mask will be preserved")
                updated_count += 1
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
        
        print("="*50)
        print(f"Updated {updated_count}/{len(selected_enabled)} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Set {updated_count} objects to preserve visibility mask")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_enable_preserve_uv(Operator):
    """Enable preserve UV for selected enabled objects"""
    bl_idname = "camprojpaint.enable_preserve_uv"
    bl_label = "Enable Preserve UV"
    bl_description = "Enable preserve UV for selected enabled objects (or all enabled if none selected)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        # If nothing selected, use all enabled objects
        if not selected_enabled:
            selected_enabled = enabled_objects
        
        if not selected_enabled:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        updated_count = 0
        
        print("\n" + "="*50)
        print("Enabling preserve UV")
        print("="*50)
        
        for obj in selected_enabled:
            try:
                if not obj.cam_proj_paint.preserve_uv:
                    obj.cam_proj_paint.preserve_uv = True
                    print(f"  {obj.name}: ✓ Preserve UV enabled")
                    updated_count += 1
                else:
                    print(f"  {obj.name}: Already enabled")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
        
        print("="*50)
        print(f"Updated {updated_count}/{len(selected_enabled)} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Enabled preserve UV on {updated_count} objects")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_disable_preserve_uv(Operator):
    """Disable preserve UV for selected enabled objects"""
    bl_idname = "camprojpaint.disable_preserve_uv"
    bl_label = "Disable Preserve UV"
    bl_description = "Disable preserve UV for selected enabled objects (or all enabled if none selected)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        # If nothing selected, use all enabled objects
        if not selected_enabled:
            selected_enabled = enabled_objects
        
        if not selected_enabled:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        updated_count = 0
        
        print("\n" + "="*50)
        print("Disabling preserve UV")
        print("="*50)
        
        for obj in selected_enabled:
            try:
                if obj.cam_proj_paint.preserve_uv:
                    obj.cam_proj_paint.preserve_uv = False
                    print(f"  {obj.name}: ✓ Preserve UV disabled")
                    updated_count += 1
                else:
                    print(f"  {obj.name}: Already disabled")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
        
        print("="*50)
        print(f"Updated {updated_count}/{len(selected_enabled)} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Disabled preserve UV on {updated_count} objects")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_sync_projection_frame(Operator):
    """Sync projection frame settings from active object to all selected enabled objects"""
    bl_idname = "camprojpaint.sync_projection_frame"
    bl_label = "Sync Projection Frame"
    bl_description = "Apply active object's projection frame settings to all selected enabled objects"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        active_obj = context.active_object
        
        if not active_obj:
            self.report({'WARNING'}, "No active object")
            return {'CANCELLED'}
        
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        if not selected_enabled:
            self.report({'WARNING'}, "No selected enabled objects")
            return {'CANCELLED'}
        
        if active_obj not in enabled_objects:
            self.report({'WARNING'}, "Active object is not enabled")
            return {'CANCELLED'}
        
        # Get settings from active object
        use_frame = active_obj.cam_proj_paint.use_projection_frame
        frame = active_obj.cam_proj_paint.projection_frame
        
        synced_count = 0
        
        print("\n" + "="*50)
        print(f"Syncing projection frame settings: use_frame={use_frame}, frame={frame}")
        print("="*50)
        
        for obj in selected_enabled:
            if obj == active_obj:
                continue  # Skip active object
            
            try:
                obj.cam_proj_paint.use_projection_frame = use_frame
                obj.cam_proj_paint.projection_frame = frame
                synced_count += 1
                print(f"  {obj.name}: ✓ Synced")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
        
        print("="*50)
        print(f"Synced {synced_count}/{len(selected_enabled)-1} objects")
        print("="*50 + "\n")
        
        self.report({'INFO'}, f"Synced projection frame to {synced_count} objects")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_setup_preview_materials(Operator):
    """Setup preview materials for projection painting"""
    bl_idname = "camprojpaint.setup_preview_materials"
    bl_label = "Setup Preview Materials"
    bl_description = "Setup material nodes to preview projection painting in viewport"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        scene = context.scene
        settings = scene.cam_proj_paint
        
        processed_count = 0
        total_materials = 0
        total_texture_nodes = 0
        
        # Parse ignore prefixes once
        ignore_prefixes = parse_ignore_prefixes(scene)
        if ignore_prefixes:
            print(f"Ignoring materials with prefixes: {ignore_prefixes}")
            
        print("\n" + "="*50)
        
        print("Setting up preview materials")
        print("="*50)
        
        for obj in enabled_objects:
            obj_materials_processed = 0
            obj_texture_nodes_processed = 0
            
            try:
                # Handle frame switching based on use_projection_frame toggle
                original_frame = context.scene.frame_current
                frame_switched = False
                
                if obj.cam_proj_paint.use_projection_frame and obj.cam_proj_paint.projection_frame >= 0:
                    # Use the stored projection frame
                    if context.scene.frame_current != obj.cam_proj_paint.projection_frame:
                        context.scene.frame_set(obj.cam_proj_paint.projection_frame)
                        frame_switched = True
                        print(f"  {obj.name}: Using stored projection frame {obj.cam_proj_paint.projection_frame}")
                
                # Ensure projection UVs are setup for this object (unless preserve_uv is enabled)
                if not obj.cam_proj_paint.preserve_uv:
                    setup_obj_projection_uv_and_visibility(obj, context.scene.camera, context)
                else:
                    print(f"  {obj.name}: Skipping UV update (Preserve UV enabled)")
                
                # Restore original frame if we switched
                if frame_switched:
                    context.scene.frame_set(original_frame)
                
                # Skip objects that already contain preview materials
                try:
                    if any((m and m.name.endswith(common.PREVIEW_MAT_SUFFIX)) for m in obj.data.materials):
                        print(f"  {obj.name}: Skipped (already has preview material)")
                        continue
                except Exception:
                    pass
                
                # Restore original materials
                if hasattr(obj, 'cam_proj_paint') and obj.cam_proj_paint and obj.cam_proj_paint.material_data:
                    restore_original_materials(obj)
                
                # Ensure material data entry exists
                ensure_obj_material_data(obj, ignore_prefixes)
                
                print(f"\n  {obj.name}:")
                
                # Process each material slot
                for mat_index, mat_slot in enumerate(obj.material_slots):
                    original_mat = mat_slot.material
                    if not original_mat:
                        continue
                    
                    # Skip materials with ignored prefixes
                    if ignore_prefixes and common.match_prefixes(original_mat.name, ignore_prefixes):
                        print(f"    Slot {mat_index}: Skipped '{original_mat.name}' (ignored prefix)")
                        continue
                    
                    if not original_mat.use_nodes:
                        print(f"    Slot {mat_index}: Skipped '{original_mat.name}' (no nodes)")
                        continue
                    
                    mat_data = get_mat_data_by_id(obj, mat_index)

                    if not mat_data:
                        print(f"    Slot {mat_index}: '{original_mat.name}' - No material data found, skipping")
                        continue
                    
                    if not mat_data.texture_nodes:
                        print(f"    Slot {mat_index}: '{original_mat.name}' - No texture nodes found, skipping")
                        continue
                    
                    # Find all image texture nodes to process
                    texture_nodes_to_process = [(tn, tn.node_name) for tn in mat_data.texture_nodes]
                    
                    # Find or create preview material
                    preview_mat = None
                    preview_mat_name = f"{original_mat.name}{common.PREVIEW_MAT_SUFFIX}"
                    if preview_mat_name in bpy.data.materials:
                        preview_mat = bpy.data.materials[preview_mat_name]
                        # Replace the material slot with the preview material and continue
                        obj.data.materials[mat_index] = preview_mat
                        mat_data.preview_material_name = preview_mat.name
                        print(f"    Slot {mat_index}: '{preview_mat.name}' - ✓ Reused existing preview material")
                        continue
                    else:
                        # Create preview material by duplicating the original
                        preview_mat = original_mat.copy()
                        preview_mat.name = preview_mat_name
                    
                    # Preserve original material
                    try:
                        original_mat.use_fake_user = True
                    except Exception:
                        pass
                    
                    # Replace the material slot with the preview material
                    obj.data.materials[mat_index] = preview_mat
                    mat_data.preview_material_name = preview_mat.name
                    
                    # Setup projection mix for each texture node
                    nodes_success = 0
                    for tex_node_data, node_name in texture_nodes_to_process:
                        # Find the texture node in the preview material
                        preview_tex_node = preview_mat.node_tree.nodes.get(node_name)
                        if not preview_tex_node or preview_tex_node.type != 'TEX_IMAGE':
                            print(f"      Node '{node_name}': ✗ Not found in preview material")
                            continue
                        
                        # Determine which projection image to use
                        projection_image = None
                        if tex_node_data and tex_node_data.psd_layer_name and tex_node_data.psd_layer_name != 'NONE':
                            print(f"      Node '{node_name}': Extracting PSD layer '{tex_node_data.psd_layer_name}'")
                            # Extract PSD layer
                            if settings.projection_psd_file:
                                import os
                                if os.path.exists(settings.projection_psd_file):
                                    layer_img_name = f"PSD_{tex_node_data.psd_layer_name}"
                                    projection_image = psd_handler.extract_single_layer(
                                        settings.projection_psd_file,
                                        tex_node_data.psd_layer_name,
                                        as_blender_image=True,
                                        image_name=layer_img_name
                                    )
                                    if projection_image:
                                        print(f"      Node '{node_name}': Extracted PSD layer '{tex_node_data.psd_layer_name}'")
                                    else:
                                        print(f"      Node '{node_name}': ✗ Failed to extract PSD layer '{tex_node_data.psd_layer_name}'")
                        else:
                            projection_image = None
                            
                        # Find destination socket
                        dest_socket = None
                        if preview_tex_node.outputs['Color'].links:
                            dest_socket = preview_tex_node.outputs['Color'].links[0].to_socket
                        
                        # Setup projection mix and get the projection texture node name and filter node name
                        projection_tex_node_name, projection_filter_node_name = setup_projection_mix(
                            preview_mat,
                            preview_tex_node,
                            projection_image,
                            context,
                            dest_socket=dest_socket,
                            tex_node_data=tex_node_data
                        )
                        
                        if projection_tex_node_name and projection_filter_node_name:
                            # Store the projection texture node name and filter node name for later use
                            tex_node_data.projection_texture_node_name = projection_tex_node_name
                            tex_node_data.projection_filter_mix_node_name = projection_filter_node_name
                            
                            nodes_success += 1
                            obj_texture_nodes_processed += 1
                            total_texture_nodes += 1
                            
                            layer_info = f" (PSD: {tex_node_data.psd_layer_name})" if (tex_node_data and tex_node_data.psd_layer_name) else ""
                            print(f"      Node '{node_name}': ✓ Setup complete{layer_info}")
                        else:
                            print(f"      Node '{node_name}': ✗ Failed to setup projection mix")
                    
                    if nodes_success > 0:
                        print(f"    Slot {mat_index}: '{preview_mat.name}' - ✓ {nodes_success} texture node(s) setup")
                        obj_materials_processed += 1
                        total_materials += 1
                    else:
                        print(f"    Slot {mat_index}: '{preview_mat.name}' - ✗ No texture nodes setup successfully")
                        # Restore original material on failure
                        obj.data.materials[mat_index] = original_mat
                        common.remove_material(preview_mat)
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                traceback.print_exc()
                self.report({'WARNING'}, f"Failed to setup preview materials for {obj.name}: {str(e)}")
                continue
                
            if obj_materials_processed > 0:
                print(f"  {obj.name}: ✓ {obj_materials_processed} material(s), {obj_texture_nodes_processed} texture node(s)")
                processed_count += 1
        
        print("="*50)
        print(f"Setup {total_materials} material(s), {total_texture_nodes} texture node(s)")
        print(f"Processed {processed_count}/{len(enabled_objects)} objects")
        print("="*50 + "\n")
        
        if processed_count > 0:
            self.report({'INFO'}, f"Preview materials setup: {len(enabled_objects)} objects, {total_materials} materials, {total_texture_nodes} texture nodes")
        else:
            self.report({'INFO'}, "Updated projection UVs and visibility")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_setup_bake_materials(Operator):
    """Setup bake materials for projection painting"""
    bl_idname = "camprojpaint.setup_bake_materials"
    bl_label = "Setup Bake Materials"
    bl_description = "Setup material nodes and prepare for baking projection texture onto original texture"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects for material setup
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        scene = context.scene
        settings = scene.cam_proj_paint
        bake_method = settings.bake_method
        
        processed_obj_count = 0
        total_materials = 0
        total_tex_nodes = 0
        total_new_bake_materials = 0 
        # Parse ignore prefixes once
        ignore_prefixes = parse_ignore_prefixes(context.scene)
        if ignore_prefixes:
            print(f"Ignoring materials with prefixes: {ignore_prefixes}")
        
        print("\n" + "="*50)
        print(f"Setting up bake materials")
        print("="*50)
        
        for obj in enabled_objects:
            obj_materials_processed = 0
            mat_bake_materials_created = 0
            try:
                # Handle frame switching based on use_projection_frame toggle
                original_frame = context.scene.frame_current
                frame_switched = False
                
                if obj.cam_proj_paint.use_projection_frame and obj.cam_proj_paint.projection_frame >= 0:
                    # Use the stored projection frame
                    if context.scene.frame_current != obj.cam_proj_paint.projection_frame:
                        context.scene.frame_set(obj.cam_proj_paint.projection_frame)
                        frame_switched = True
                        print(f"  {obj.name}: Using stored projection frame {obj.cam_proj_paint.projection_frame}")
                
                # Ensure projection UVs are setup for this object (unless preserve_uv is enabled)
                if not obj.cam_proj_paint.preserve_uv:
                    setup_obj_projection_uv_and_visibility(obj, context.scene.camera, context)
                else:
                    print(f"  {obj.name}: Skipping UV update (Preserve UV enabled)")
                
                # Restore original frame if we switched
                if frame_switched:
                    context.scene.frame_set(original_frame)
                
                # Get rid of any existing preview/bake materials and restore originals
                if hasattr(obj, 'cam_proj_paint') and obj.cam_proj_paint and obj.cam_proj_paint.material_data:
                    restore_original_materials(obj)
                    
                # Ensure material data entry exists
                ensure_obj_material_data(obj, ignore_prefixes)
                
                print(f"\n  {obj.name}:")
                
                # Create one bake material per texture node
                for mat_data in obj.cam_proj_paint.material_data:
                    original_mat = bpy.data.materials.get(mat_data.original_material_name)
                    if not original_mat:
                        continue
                    
                    total_materials += 1
                    
                    mat_index = mat_data.material_index
                    print(f"    Slot {mat_index}: '{original_mat.name}'")
                    
                    for tex_node_data in mat_data.texture_nodes:
                        total_tex_nodes += 1
                        # Check if this texture node already has a bake material
                        if tex_node_data.bake_material_name:
                            existing_bake_mat = bpy.data.materials.get(tex_node_data.bake_material_name)
                            if existing_bake_mat:
                                print(f"      Node '{tex_node_data.node_name}': Skipped (bake material already exists)")
                                continue
                        
                        # Find the original texture node in the original material
                        orig_tex_node = original_mat.node_tree.nodes.get(tex_node_data.node_name)
                        if not orig_tex_node or orig_tex_node.type != 'TEX_IMAGE':
                            print(f"      Node '{tex_node_data.node_name}': ✗ Not found or not a texture node")
                            continue
                        
                        original_texture_image = orig_tex_node.image
                        if not original_texture_image:
                            print(f"      Node '{tex_node_data.node_name}': ✗ No image assigned")
                            continue
                        
                        # Create unique bake material for this specific texture node
                        bake_mat_name = f"{obj.name}__{original_mat.name}__{tex_node_data.node_name}{common.BAKE_MAT_SUFFIX}"
                        bake_mat = bpy.data.materials.new(name=bake_mat_name)
                        bake_mat.use_nodes = True
                        bake_mat.use_fake_user = True  # Prevent deletion since not assigned to any slot
                        tex_node_data.bake_material_name = bake_mat.name
                        
                        # Clear default nodes
                        bake_nodes = bake_mat.node_tree.nodes
                        bake_nodes.clear()
                        links = bake_mat.node_tree.links
                        
                        # Get projection image (extract PSD layer if assigned)
                        projection_image = None
                        if tex_node_data.psd_layer_name and tex_node_data.psd_layer_name != 'NONE':
                            if settings.projection_psd_file and os.path.exists(settings.projection_psd_file):
                                layer_img_name = f"PSD_{tex_node_data.psd_layer_name}"
                                projection_image = psd_handler.extract_single_layer(
                                    settings.projection_psd_file,
                                    tex_node_data.psd_layer_name,
                                    as_blender_image=True,
                                    image_name=layer_img_name
                                )
                        else:
                            projection_image = None
                        
                        # Create original texture node
                        original_tex_node = bake_nodes.new(type='ShaderNodeTexImage')
                        original_tex_node.name = common.NODE_NAME_ORIGINAL_TEXTURE
                        original_tex_node.label = common.node_name_to_label(common.NODE_NAME_ORIGINAL_TEXTURE)
                        original_tex_node.image = original_texture_image
                        original_tex_node.location = (-400, 300)
                        
                        # Create UV Map node for original UV (if specified)
                        if tex_node_data.original_uv_map:
                            original_uv_node = bake_nodes.new(type='ShaderNodeUVMap')
                            original_uv_node.name = common.NODE_NAME_ORIGINAL_UV
                            original_uv_node.label = common.node_name_to_label(common.NODE_NAME_ORIGINAL_UV)
                            original_uv_node.uv_map = tex_node_data.original_uv_map
                            original_uv_node.location = (-800, 300)
                            links.new(original_uv_node.outputs['UV'], original_tex_node.inputs['Vector'])
                        
                        # Create Material Output node
                        output_node = bake_nodes.new(type='ShaderNodeOutputMaterial')
                        output_node.location = (600, 0)
                        
                        # Determine destination socket based on bake method
                        if bake_method == 'EEVEE':
                            # EEVEE: Connect mix output directly to Material Output
                            dest_socket = output_node.inputs['Surface']
                        else:  # CYCLES
                            # CYCLES: Must use Principled BSDF for baking to work
                            bake_principled = bake_nodes.new(type='ShaderNodeBsdfPrincipled')
                            bake_principled.name = common.NODE_NAME_BAKE_PRINCIPLED
                            bake_principled.label = common.node_name_to_label(common.NODE_NAME_BAKE_PRINCIPLED)
                            bake_principled.location = (300, 0)
                            
                            # Connect Bake Principled to Output
                            links.new(bake_principled.outputs['BSDF'], output_node.inputs['Surface'])
                            dest_socket = bake_principled.inputs['Base Color']
                        
                        # Use setup_projection_mix to create the projection mixing nodes
                        if projection_image:
                            projection_tex_node_name, projection_filter_node_name = setup_projection_mix(
                                bake_mat,
                                original_tex_node,
                                projection_image,
                                context,
                                dest_socket=dest_socket,
                                tex_node_data=tex_node_data
                            )
                            
                            if not projection_tex_node_name or not projection_filter_node_name:
                                print(f"      Node '{tex_node_data.node_name}': ✗ Failed to setup projection mix")
                                continue
                            
                            # Store the projection texture node name and filter node name for later use
                            tex_node_data.projection_texture_node_name = projection_tex_node_name
                            tex_node_data.projection_filter_mix_node_name = projection_filter_node_name
                        else:
                            # No projection image, just connect original texture to destination
                            links.new(original_tex_node.outputs['Color'], dest_socket)
                        
                        # Create temporary bake target image (same size as original)
                        if original_texture_image:
                            width = original_texture_image.size[0]
                            height = original_texture_image.size[1]
                        else:
                            width = 2048
                            height = 2048
                        
                        bake_target_name = f"{obj.name}__{original_mat.name}__{tex_node_data.node_name}{common.BAKE_TARGET_IMG_SUFFIX}"
                        bake_target_tex = bpy.data.images.get(bake_target_name)
                        
                        if bake_target_tex:
                            # Reuse existing image, but ensure it has the correct size
                            if bake_target_tex.size[0] != width or bake_target_tex.size[1] != height:
                                bake_target_tex.scale(width, height)
                        else:
                            # Create new transparent bake target image
                            bake_target_tex = common.create_image(
                                name=bake_target_name,
                                width=width,
                                height=height
                            )
                        
                        tex_node_data.bake_target_texture = bake_target_tex
                        
                        # Create Image Texture node for bake target
                        bake_target_node = bake_nodes.new(type='ShaderNodeTexImage')
                        bake_target_node.name = common.NODE_NAME_BAKE_TARGET
                        bake_target_node.label = common.node_name_to_label(common.NODE_NAME_BAKE_TARGET)
                        bake_target_node.image = bake_target_tex
                        bake_target_node.location = (300, -300)
                        
                        # Preserve original material
                        try:
                            if original_mat:
                                original_mat.use_fake_user = True
                        except Exception:
                            pass
                        
                        layer_info = f" (PSD: {tex_node_data.psd_layer_name})" if tex_node_data.psd_layer_name else ""
                        print(f"      Node '{tex_node_data.node_name}': ✓ Bake material created{layer_info}")
                        mat_bake_materials_created += 1
                        total_new_bake_materials += 1
                    
                    if mat_bake_materials_created > 0:
                        obj_materials_processed += 1
                        print(f"    Slot {mat_index}: '{original_mat.name}' - ✓ {mat_bake_materials_created} bake material(s) created")
                    else:
                        print(f"    Slot {mat_index}: '{original_mat.name}' - ✗ No bake materials created")
                
                if obj_materials_processed == 0:
                    print(f"  {obj.name}: ✗ No materials processed")
                    continue
                
                print(f"  {obj.name}: ✓ Setup complete - {obj_materials_processed} material(s)")

                processed_obj_count += 1
                
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                import traceback
                traceback.print_exc()
                self.report({'WARNING'}, f"Failed to setup bake materials for {obj.name}: {str(e)}")
        
        print("="*50)
        print(f"Setup {total_materials} material(s), {total_tex_nodes} texture node(s) on {processed_obj_count}/{len(enabled_objects)} objects")
        print(f"Created {total_new_bake_materials} new bake material(s)")
        print("="*50 + "\n")
        
        if total_new_bake_materials > 0:
            self.report({'INFO'}, f"Bake materials setup: {len(enabled_objects)} objects, {total_materials} materials, {total_new_bake_materials} new bake materials created")
        else:
            self.report({'INFO'}, "Updated projection UVs and visibility")
            return {'FINISHED'}
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_reload_projection_image(Operator):
    """Reload and apply PSD layers from disk"""
    bl_idname = "camprojpaint.reload_projection_image"
    bl_label = "Reload PSD Layers"
    bl_description = "Reload the PSD file and apply updated layers to all enabled objects"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        settings = context.scene.cam_proj_paint
        psd_file_path = settings.projection_psd_file
        
        if not psd_file_path:
            self.report({'ERROR'}, "No PSD file selected")
            return {'CANCELLED'}
        
        # Check if PSD file exists
        if not os.path.exists(psd_file_path):
            self.report({'ERROR'}, f"PSD file not found: {psd_file_path}")
            return {'CANCELLED'}
        
        # Auto-map PSD layers if enabled
        if settings.auto_map_on_reload:
            print("\n" + "="*50)
            print("Auto-mapping PSD layers before reload...")
            print("="*50)
            
            enabled_objects = common.get_enabled_objects(context)
            if enabled_objects:
                mapped_count, total_nodes = auto_map_psd_layers_to_textures(
                    context,
                    psd_file_path,
                    enabled_objects,
                    verbose=True
                )
                print(f"Auto-mapped {mapped_count}/{total_nodes} texture nodes")
                print("="*50)
        
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'INFO'}, f"PSD file exists but no enabled objects to apply to")
            return {'FINISHED'}
        
        # Apply to all enabled objects
        updated_count = 0
        total_texture_nodes = 0
        
        print("\n" + "="*50)
        print(f"Reloading PSD layers from: {os.path.basename(psd_file_path)}")
        print("="*50)
        
        for obj in enabled_objects:
            try:
                obj_texture_nodes_updated = 0
                
                for mat_data in obj.cam_proj_paint.material_data:
                    # Iterate through each texture node
                    for tex_node_data in mat_data.texture_nodes:
                        # Skip if no PSD layer assigned
                        if not tex_node_data.psd_layer_name or tex_node_data.psd_layer_name == 'NONE':
                            continue
                        
                        # Extract the PSD layer
                        try:
                            layer_image = psd_handler.extract_single_layer(
                                psd_file_path,
                                tex_node_data.psd_layer_name,
                                as_blender_image=True,
                                image_name=f"PSD_{tex_node_data.psd_layer_name}"
                            )
                            
                            if not layer_image:
                                print(f"    Node '{tex_node_data.node_name}': ✗ Failed to extract layer '{tex_node_data.psd_layer_name}'")
                                continue
                            
                        except Exception as e:
                            print(f"    Node '{tex_node_data.node_name}': ✗ Error extracting layer - {str(e)}")
                            continue
                        
                        # Update bake material if it exists
                        bake_mat = bpy.data.materials.get(tex_node_data.bake_material_name)
                        if bake_mat and bake_mat.use_nodes:
                            nodes = bake_mat.node_tree.nodes
                            projection_tex_node = nodes.get(tex_node_data.projection_texture_node_name)
                            
                            if projection_tex_node:
                                # Delete old layer image if it does not correspond to any layer in the PSD file anymore
                                old_image = projection_tex_node.image
                                if old_image and not is_image_a_psd_layer(old_image, psd_file_path):
                                    print(f"      Removing obsolete PSD layer image: '{old_image.name}'")
                                    common.remove_image(old_image)
                                
                                projection_tex_node.image = layer_image
                            else:
                                print(f"    Node '{tex_node_data.node_name}': ✗ Projection texture node '{tex_node_data.projection_texture_node_name}' not found in bake material")
                        
                        # Update preview material if it exists
                        preview_mat = bpy.data.materials.get(mat_data.preview_material_name)
                        if preview_mat and preview_mat.use_nodes:
                            nodes = preview_mat.node_tree.nodes
                            projection_tex_node = nodes.get(tex_node_data.projection_texture_node_name)
                            
                            if projection_tex_node:
                                # Delete old layer image if it does not correspond to any layer in the PSD file anymore
                                old_image = projection_tex_node.image
                                if old_image and not is_image_a_psd_layer(old_image, psd_file_path):
                                    print(f"      Removing obsolete PSD layer image: '{old_image.name}'")
                                    common.remove_image(old_image)
                                
                                projection_tex_node.image = layer_image
                            else:
                                print(f"    Node '{tex_node_data.node_name}': ✗ Projection texture node '{tex_node_data.projection_texture_node_name}' not found in preview material")
                        
                        print(f"    Node '{tex_node_data.node_name}': ✓ Reloaded layer '{tex_node_data.psd_layer_name}'")
                        obj_texture_nodes_updated += 1
                
                if obj_texture_nodes_updated > 0:
                    total_texture_nodes += obj_texture_nodes_updated
                    print(f"  {obj.name}: ✓ Updated {obj_texture_nodes_updated} texture node(s)")
                    updated_count += 1
                else:
                    print(f"  {obj.name}: ✗ No texture nodes updated")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                traceback.print_exc()
        
        print("="*50)
        print(f"Reloaded {total_texture_nodes} texture node(s) on {updated_count}/{len(enabled_objects)} objects")
        print("="*50 + "\n")
        
        if total_texture_nodes > 0:
            self.report({'INFO'}, f"Reloaded {total_texture_nodes} PSD layers on {updated_count} objects")
        else:
            self.report({'WARNING'}, "No PSD layers were reloaded")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_bake_projection(Operator):
    """Bake the projection texture onto the original textures"""
    bl_idname = "camprojpaint.bake_projection"
    bl_label = "Bake Projection"
    bl_description = "Bake the mixed projection onto original textures for all enabled objects"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Gather enabled objects for baking
        enabled_objects = common.get_enabled_objects(context)

        if not enabled_objects:
            self.report({'WARNING'}, "No objects ready for baking. Run 'Setup Bake' first")
            return {'CANCELLED'}

        # Check if any enabled objects have bake materials set up
        has_bake_materials = False
        for obj in enabled_objects:
            for mat_data in obj.cam_proj_paint.material_data:
                if any(tn.bake_material_name for tn in mat_data.texture_nodes):
                    has_bake_materials = True
                    break
        
        if not has_bake_materials:
            self.report({'ERROR'}, "No bake materials found. Run 'Setup Bake Materials' first")
            return {'CANCELLED'}

        # Ensure Cycles is active for baking
        original_engine = context.scene.render.engine
        switched = False
        if original_engine != 'CYCLES':
            context.scene.render.engine = 'CYCLES'
            switched = True
            print(f"Switched render engine from {original_engine} to CYCLES")

        try:
            # Route to the appropriate bake core function
            baked_count, total_items = bake_projection_core_psd(context, enabled_objects, cycles_backend_psd)
            item_type = "texture nodes"
        finally:
            if switched:
                try:
                    context.scene.render.engine = original_engine
                    print(f"Restored render engine to {original_engine}")
                except Exception:
                    pass

        if baked_count > 0:
            self.report({'INFO'}, f"Baked {total_items} {item_type} on {baked_count} objects. Now run 'Apply Baked Result'")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Failed to bake any objects")
            return {'CANCELLED'}

class CAMPROJPAINT_OT_bake_projection_eevee(Operator):
    """Bake the projection texture using EEVEE rendering (fast)"""
    bl_idname = "camprojpaint.bake_projection_eevee"
    bl_label = "Bake Projection (EEVEE)"
    bl_description = "Bake the mixed projection using EEVEE rendering - faster than Cycles"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects that have material data with bake setup
        enabled_objects = common.get_enabled_objects(context)

        if not enabled_objects:
            self.report({'WARNING'}, "No objects ready for baking. Run 'Setup Bake' first")
            return {'CANCELLED'}

        # Check if any enabled objects have bake materials set up
        has_bake_materials = False
        for obj in enabled_objects:
            for mat_data in obj.cam_proj_paint.material_data:
                if any(tn.bake_material_name for tn in mat_data.texture_nodes):
                    has_bake_materials = True
                    break
        
        if not has_bake_materials:
            self.report({'ERROR'}, "No bake materials found. Run 'Setup Bake Materials' first")
            return {'CANCELLED'}

        # Delegate to shared core using the EEVEE backend and appropriate bake core
        baked_count, total_items = bake_projection_core_psd(context, enabled_objects, eevee_backend_psd)
        item_type = "texture nodes"

        if baked_count > 0:
            self.report({'INFO'}, f"EEVEE baked {total_items} {item_type} on {baked_count} objects. Now run 'Apply Baked Result'")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Failed to bake any objects")
            return {'CANCELLED'}

class CAMPROJPAINT_OT_apply_baked_result(Operator):
    """Copy the baked result to the original textures"""
    bl_idname = "camprojpaint.apply_baked_result"
    bl_label = "Apply Baked Result"
    bl_description = "Copy the baked result from temp images to original textures"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects for baking
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No baked images found. Run 'Bake Projection' first")
            return {'CANCELLED'}
        
        applied_count = 0
        total_tex_nodes_applied = 0  # Materials in single-texture mode, texture nodes in PSD mode
        
        print("\n" + "="*50)
        print("Applying baked results to original textures (PSD Multi-Texture Mode)")
        print("="*50)
        
        for obj in enabled_objects:
            try:
                obj_items_applied = 0
                
                # Iterate through each material data
                for mat_data in obj.cam_proj_paint.material_data:
                    
                    if not mat_data.texture_nodes:
                        continue
                    
                    for tex_node_data in mat_data.texture_nodes:
                        bake_target_tex = tex_node_data.bake_target_texture
                        original_img = tex_node_data.original_texture
                        
                        if not bake_target_tex or not original_img:
                            print(f"    Node '{tex_node_data.node_name}': ✗ Missing images")
                            continue
                        
                        # Alpha compositing from bake target to original texture
                        width = bake_target_tex.size[0]
                        height = bake_target_tex.size[1]
                        layer_info = f" (PSD: {tex_node_data.psd_layer_name})" if tex_node_data.psd_layer_name else ""
                        print(f"    Node '{tex_node_data.node_name}'{layer_info}: Applying '{bake_target_tex.name}' → '{original_img.name}' ({width}x{height})")
                        
                        common.alpha_composite_images(bake_target_tex, original_img)
                        
                        # Update the image
                        original_img.update()
                        original_img.update_tag()
                        
                        print(f"    Node '{tex_node_data.node_name}': ✓ Applied to '{original_img.name}'")
                        obj_items_applied += 1
                        total_tex_nodes_applied += 1
                
                if obj_items_applied > 0:
                    print(f"  {obj.name}: ✓ Applied {obj_items_applied} texture node(s)")
                    applied_count += 1
                else:
                    print(f"  {obj.name}: ✗ No items applied")
                
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                import traceback
                traceback.print_exc()
                self.report({'WARNING'}, f"Failed to apply result for {obj.name}: {str(e)}")
        
        print("="*50)
        print(f"Applied {total_tex_nodes_applied} texture nodes on {applied_count}/{len(enabled_objects)} objects")
        print("="*50 + "\n")
        
        # Force viewport refresh to update texture display in all shading modes
        common.refresh_viewport(context)
        
        if applied_count > 0:
            self.report({'INFO'}, f"Applied {total_tex_nodes_applied} texture nodes on {applied_count} objects. Run 'Cleanup' to finish")
        else:
            self.report({'ERROR'}, "Failed to apply any baked results")
            return {'CANCELLED'}
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_remove_temp_materials(Operator):
    """Remove temporary materials and textures, restore original materials"""
    bl_idname = "camprojpaint.remove_temp_materials"
    bl_label = "Remove Temp Materials"
    bl_description = "Remove temporary preview/bake materials and textures, restore original materials (preserves projection UV map, vertex color, and PSD layer mapping)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects for cleanup
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects to clean up")
            return {'CANCELLED'}

        cleaned_count = 0
        total_materials_cleaned = 0
        total_items_cleaned = 0 
        
        print("\n" + "="*50)
        print("Removing temporary materials and textures (preserving PSD layer mapping)")
        print("="*50)
        
        for obj in enabled_objects:
            try:
                obj_materials_cleaned = 0
                obj_items_cleaned = 0
                
                # Iterate through material data (no need to reverse since we're not removing items)
                for mat_data in obj.cam_proj_paint.material_data:
                    mat_index = mat_data.material_index
                    
                    # Restore original material to the slot
                    original_mat_name = mat_data.original_material_name
                    if original_mat_name:
                        original_mat = bpy.data.materials.get(original_mat_name)
                        if original_mat:
                            # Find the material slot and restore
                            if mat_index < len(obj.material_slots):
                                obj.material_slots[mat_index].material = original_mat
                                print(f"    Slot {mat_index}: Restored '{original_mat_name}'")
                    
                    # Remove preview material (same for both modes)
                    preview_mat_name = mat_data.preview_material_name
                    if preview_mat_name:
                        preview_mat = bpy.data.materials.get(preview_mat_name)
                        if preview_mat and common.remove_material(preview_mat):
                            print(f"    Slot {mat_index}: Removed preview material '{preview_mat_name}'")
                            # Clear the name reference
                            mat_data.preview_material_name = ""
                    
                    for tex_node_data in mat_data.texture_nodes:
                        # Remove bake material for this texture node
                        if tex_node_data.bake_material_name:
                            bake_mat = bpy.data.materials.get(tex_node_data.bake_material_name)
                            if bake_mat and common.remove_material(bake_mat):
                                print(f"      Node '{tex_node_data.node_name}': Removed bake material '{tex_node_data.bake_material_name}'")
                                # Clear the name reference
                                tex_node_data.bake_material_name = ""
                        
                        # Remove bake target image for this texture node
                        if tex_node_data.bake_target_texture:
                            img_name = tex_node_data.bake_target_texture.name
                            common.remove_image(tex_node_data.bake_target_texture)
                            print(f"      Node '{tex_node_data.node_name}': Removed bake target '{img_name}'")
                            # Clear the image reference
                            tex_node_data.bake_target_texture = None
                        
                        obj_items_cleaned += 1
                        total_items_cleaned += 1
                    
                    obj_materials_cleaned += 1
                    total_materials_cleaned += 1
                
                # DO NOT clear the material data collection - preserve PSD layer mapping
                # obj.cam_proj_paint.material_data.clear()
                
                # Clear object-level references
                obj.cam_proj_paint.projection_texture = None
                
                if obj_materials_cleaned > 0:
                    print(f"  {obj.name}: ✓ Cleaned up {obj_materials_cleaned} material(s), {obj_items_cleaned} texture node(s)")
                    cleaned_count += 1
                else:
                    print(f"  {obj.name}: ✗ No materials to clean up")
                
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                import traceback
                traceback.print_exc()
                self.report({'WARNING'}, f"Failed to cleanup {obj.name}: {str(e)}")
        
        print("="*50)
        print(f"Cleaned up {total_materials_cleaned} materials, {total_items_cleaned} texture nodes on {cleaned_count}/{len(enabled_objects)} objects")
        print("="*50 + "\n")
        
        # Also cleanup UV bake temporary objects and collections
        print("Cleaning up UV bake temporary data...")
        try:
            bpy.ops.uvbake.cleanup_temp()
            print("✓ UV bake cleanup complete")
        except Exception as e:
            print(f"Note: UV bake cleanup not available or failed: {str(e)}")
        
        if cleaned_count > 0:
            self.report({'INFO'}, f"Cleanup complete: {total_materials_cleaned} materials, {total_items_cleaned} texture nodes (PSD mapping preserved)")
        else:
            self.report({'ERROR'}, "Failed to cleanup any objects")
            return {'CANCELLED'}
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_clear_psd_mapping(Operator):
    """Clear PSD layer mapping data from all enabled objects"""
    bl_idname = "camprojpaint.clear_psd_mapping"
    bl_label = "Clear PSD Mapping"
    bl_description = "Remove all discovered texture nodes and PSD layer mapping data"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        cleared_count = 0
        total_materials = 0
        total_texture_nodes = 0
        
        print("\n" + "="*50)
        print("Clearing PSD layer mapping data")
        print("="*50)
        
        for obj in enabled_objects:
            try:
                # Count materials and texture nodes before clearing
                obj_materials = len(obj.cam_proj_paint.material_data)
                obj_texture_nodes = sum(len(mat_data.texture_nodes) for mat_data in obj.cam_proj_paint.material_data)
                
                if obj_materials > 0:
                    # Clear the material data collection
                    obj.cam_proj_paint.material_data.clear()
                    
                    total_materials += obj_materials
                    total_texture_nodes += obj_texture_nodes
                    cleared_count += 1
                    
                    print(f"  {obj.name}: ✓ Cleared {obj_materials} material(s), {obj_texture_nodes} texture node(s)")
                else:
                    print(f"  {obj.name}: No mapping data to clear")
                    
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                import traceback
                traceback.print_exc()
        
        print("="*50)
        print(f"Cleared {total_materials} materials, {total_texture_nodes} texture nodes from {cleared_count}/{len(enabled_objects)} objects")
        print("="*50 + "\n")
        
        if cleared_count > 0:
            self.report({'INFO'}, f"Cleared PSD mapping: {total_materials} materials, {total_texture_nodes} texture nodes")
        else:
            self.report({'WARNING'}, "No PSD mapping data found to clear")
        
        return {'FINISHED'}

class CAMPROJPAINT_OT_remove_temp_uvs_vcols(Operator):
    """Remove temporary UV maps and vertex color layers"""
    bl_idname = "camprojpaint.remove_temp_uvs_vcols"
    bl_label = "Remove Temp UVs/VCols"
    bl_description = "Remove projection UV map and vertex color from enabled objects"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Get enabled objects
        enabled_objects = common.get_enabled_objects(context)
        
        if not enabled_objects:
            self.report({'WARNING'}, "No enabled objects")
            return {'CANCELLED'}
        
        removed_count = 0
        
        print("\n" + "="*50)
        print("Removing temporary UV maps and vertex color layers")
        print("="*50)
        
        for obj in enabled_objects:
            try:
                removed_uv_or_vcol = False
                
                # Remove projection UV map
                if common.remove_uv_layer(obj, common.PROJECTION_UV_NAME):
                    removed_uv_or_vcol = True
                    print(f"  {obj.name}: Removed UV map '{common.PROJECTION_UV_NAME}'")
                        
                # Remove vertex color layer
                if common.remove_vertex_color_layer(obj, common.PROJECTION_VIS_VCOL_NAME):
                    removed_uv_or_vcol = True
                    print(f"  {obj.name}: Removed vertex color '{common.PROJECTION_VIS_VCOL_NAME}'")
                
                if removed_uv_or_vcol:
                    removed_count += 1
                    print(f"  {obj.name}: ✓ Removed temporary data")
                else:
                    print(f"  {obj.name}: No temporary data to remove")
                
            except Exception as e:
                print(f"  {obj.name}: ✗ Error - {str(e)}")
                import traceback
                traceback.print_exc()
        
        print("="*50)
        print(f"Removed temporary data from {removed_count}/{len(enabled_objects)} objects")
        print("="*50 + "\n")
        
        if removed_count > 0:
            self.report({'INFO'}, f"Removed temporary UVs/VCols from {removed_count} objects")
        else:
            self.report({'WARNING'}, "No temporary data found to remove")
        
        return {'FINISHED'}

# =====================================================================
# UI Panel
# =====================================================================

class CAMPROJPAINT_PT_main_panel(Panel):
    """Main panel for Camera Projection Paint"""
    bl_label = "Camera Projection Paint"
    bl_idname = "CAMPROJPAINT_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Illustration'
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.cam_proj_paint
        
        # Check for missing dependencies and show install button
        missing_deps = []
        if not psd_handler.is_psd_available():
            missing_deps.append("psd-tools")
        if not psd_watcher.WATCHDOG_AVAILABLE:
            missing_deps.append("watchdog")
        
        if missing_deps:
            box = layout.box()
            box.label(text="Missing Dependencies", icon='ERROR')
            box.label(text=f"Required: {', '.join(missing_deps)}", icon='INFO')
            row = box.row()
            row.scale_y = 1.5
            row.operator("camprojpaint.install_dependencies", icon='IMPORT', text="Install Dependencies")
            layout.separator()
        
        # PSD File Selection and Info
        box = layout.box()
        box.label(text="PSD File", icon='GREASEPENCIL_LAYER_GROUP')
        
        # Check if psd-tools is available
        if not psd_handler.is_psd_available():
            box.label(text="psd-tools not installed!", icon='ERROR')
            box.label(text="Install with pip to enable", icon='INFO')
        else:
            row = box.row()
            row.prop(settings, "projection_psd_file", text="")
            
            # Show PSD info if file is selected
            if settings.projection_psd_file:
                import os
                if os.path.exists(settings.projection_psd_file):
                    psd_info = psd_handler.get_psd_info(settings.projection_psd_file)
                    if psd_info:
                        box.label(text=f"Size: {psd_info['width']}x{psd_info['height']}", icon='IMAGE_DATA')
                        box.label(text=f"Layers: {psd_info['layer_count']}", icon='OUTLINER_DATA_GP_LAYER')
                    else:
                        box.label(text="Failed to read PSD file", icon='ERROR')
                else:
                    box.label(text="File not found!", icon='ERROR')
            
            # File Watcher Toggle
            box.separator()
            if psd_watcher.WATCHDOG_AVAILABLE:
                row = box.row()
                if psd_watcher.is_watching():
                    row.operator("camprojpaint.toggle_auto_reload", text="Stop Auto-Reload", icon='PAUSE', depress=True)
                else:
                    row.operator("camprojpaint.toggle_auto_reload", text="Start Auto-Reload", icon='PLAY')
                row.enabled = bool(settings.projection_psd_file)
            else:
                # Show info about watchdog if not available
                row = box.row()
                row.label(text="File Watcher: Not Installed", icon='ERROR')
                row = box.row()
                row.label(text="Install 'watchdog' for auto-reload", icon='INFO')
            
            # Reload button
            row = box.row()
            row.scale_y = 1.2
            row.operator("camprojpaint.reload_projection_image", icon='FILE_REFRESH', text="Reload PSD Layers")
            row.enabled = bool(settings.projection_psd_file)
            
            # Auto-map on reload toggle
            row = box.row()
            row.prop(settings, "auto_map_on_reload", text="Auto-Map on Reload")
        
        layout.separator()
        
        # Object Enable/Disable Controls
        box = layout.box()
        box.label(text="Enable Objects", icon='OBJECT_DATA')
        
        # Show count of enabled objects
        enabled_objects = common.get_enabled_objects(context)
        if enabled_objects:
            preview_only_count = sum(1 for obj in enabled_objects if obj.cam_proj_paint.preview_only)
            if preview_only_count > 0:
                box.label(text=f"{len(enabled_objects)} object(s) enabled ({preview_only_count} preview only)")
            else:
                box.label(text=f"{len(enabled_objects)} object(s) enabled")
        # Show if active object has preview materials
        active_obj = context.active_object
        if active_obj and object_has_preview_materials(active_obj):
            if active_obj.cam_proj_paint.preview_only:
                box.label(text="Active is preview only", icon='HIDE_ON')
            else:
                box.label(text="Active is being previewed", icon='CHECKMARK')
        elif active_obj in enabled_objects:
            if active_obj.cam_proj_paint.preview_only:
                box.label(text="Active is preview only", icon='HIDE_ON')
            else:
                box.label(text="Active is enabled", icon='CHECKMARK')
        
        # Enable visible from camera button
        row = box.row()
        row.operator("camprojpaint.enable_visible_objects", icon='CAMERA_DATA', text="Enable Visible from Camera")
        
        # Enable/Disable selected buttons
        row = box.row()
        row.label(text="Enable (Selected Objects):", icon='RESTRICT_VIEW_OFF')
        row = box.row(align=True)
        row.operator("camprojpaint.enable_selected_objects", text="Enable", icon="RESTRICT_VIEW_OFF")
        row.operator("camprojpaint.disable_selected_objects", text="Disable", icon="RESTRICT_VIEW_ON")
        
        # Select enabled objects button
        row = box.row()
        row.operator("camprojpaint.select_enabled_objects", icon='RESTRICT_SELECT_OFF', text="Select Enabled")

        layout.separator()
        
        # Preserve UV Controls (Selected Objects)
        if enabled_objects:
            box = layout.box()
            box.label(text="Preserve UV (Selected Objects):", icon='UV')
            
            # Two button layout
            row = box.row(align=True)
            row.operator("camprojpaint.enable_preserve_uv", icon='CHECKBOX_HLT', text="Enable")
            row.operator("camprojpaint.disable_preserve_uv", icon='CHECKBOX_DEHLT', text="Disable")
        
        # Count enabled objects
        enabled_objects = common.get_enabled_objects(context)
        if enabled_objects:
            box = layout.box()
            box.label(text="Mask (Selected Objects):", icon='MOD_MASK')
            
            # Two button layout
            row = box.row(align=True)
            row.operator("camprojpaint.set_project_visible", icon='MESH_CUBE', text="Visible")
            row.operator("camprojpaint.set_project_all", icon='HIDE_OFF', text="All")
            row.operator("camprojpaint.set_project_preserve", icon='FREEZE', text="Preserve")
        
        layout.separator()
        
        # Setup Materials for Baking (merged Step 2 & 3)
        box = layout.box()
        box.label(text="Setup Projection", icon='MATERIAL')
        
        # Bake method selection
        row = box.row()
        row.prop(settings, "bake_method", expand=True)
        
        row = box.row()
        row.prop(settings, "mix_offset")
        
        # Setup preview materials button
        row = box.row()
        row.scale_y = 1.5
        row.operator("camprojpaint.setup_preview_materials", icon='SHADING_TEXTURE', text="Update Preview")
        row.enabled = len(enabled_objects) > 0
        
        # Setup bake materials button
        row = box.row()
        row.scale_y = 1.5
        row.operator("camprojpaint.setup_bake_materials", icon='SHADING_RENDERED', text="Update Bake")
        row.enabled = len(enabled_objects) > 0
        
        # Use projection frame toggle - for selected enabled objects
        active_obj = context.active_object
        selected_enabled = [obj for obj in context.selected_objects if obj in enabled_objects]
        
        if active_obj and active_obj in enabled_objects and selected_enabled:
            row = box.row()
            row.prop(active_obj.cam_proj_paint, "use_projection_frame", text="Use Stored Projection Frame")
            
            if active_obj.cam_proj_paint.use_projection_frame:
                row = box.row()
                row.prop(active_obj.cam_proj_paint, "projection_frame", text="Frame")
                
                if active_obj.cam_proj_paint.projection_frame >= 0:
                    box.label(text=f"Will use frame {active_obj.cam_proj_paint.projection_frame}", icon='INFO')
                else:
                    box.label(text="No projection frame stored yet", icon='ERROR')
            
            # Apply button to sync settings to all selected
            if len(selected_enabled) > 1:
                row = box.row()
                row.operator("camprojpaint.sync_projection_frame", icon='COPYDOWN', text=f"Apply to {len(selected_enabled)} Selected")
        
        # Ignored material prefixes (user editable)
        box.separator()
        box.label(text="Ignored Material Prefixes", icon='FILTER')
        row = box.row()
        row.prop(settings, "ignore_material_prefixes", text="Prefixes")
        box.label(text="Separate with ,", icon='INFO')

        layout.separator()
        
        # Bake Projection button
        box = layout.box()
        box.label(text="Bake Projecetion", icon='SCENE')
        
        # Bake button - changes based on selected method
        bake_method = settings.bake_method
        row = box.row()
        row.scale_y = 1.5
        
        if bake_method == 'EEVEE':
            row.operator("camprojpaint.bake_projection_eevee", icon='RENDER_STILL', text="Bake with EEVEE")
        else:  # CYCLES
            row.operator("camprojpaint.bake_projection", icon='RENDER_STILL', text="Bake with Cycles")
        
        # Check if bake is set up
        bake_ready = has_bake_materials_ready(enabled_objects)
        row.enabled = bake_ready
        
        layout.separator()
        
        # Apply and Cleanup buttons
        box = layout.box()
        box.label(text="Finalize", icon='OUTPUT')
        
        row = box.row()
        row.scale_y = 1.5
        row.operator("camprojpaint.apply_baked_result", icon='CHECKMARK', text="Apply Baked Result")
        # Check if any material has bake_target_texture (using helper function)
        row.enabled = has_bake_targets_ready(enabled_objects)
        
        row = box.row()
        row.operator("camprojpaint.remove_temp_materials", icon='TRASH', text="Remove Temp Mats/Texs")
        row.enabled = len(enabled_objects) > 0
        
        row = box.row()
        row.operator("camprojpaint.remove_temp_uvs_vcols", icon='UV', text="Remove Temp UVs/VCols")
        row.enabled = len(enabled_objects) > 0
        
        # PSD mapping management
        box.separator()
        box.label(text="PSD Layer Mapping", icon='LINKED')
        row = box.row()
        row.operator("camprojpaint.clear_psd_mapping", icon='UNLINKED', text="Clear PSD Mapping")
        # Enable if any object has material_data
        has_mapping = any(len(obj.cam_proj_paint.material_data) > 0 for obj in enabled_objects)
        row.enabled = has_mapping
        
        layout.separator()

class CAMPROJPAINT_PT_object_panel(Panel):
    """Per-object Projection Paint settings in Object Properties"""
    bl_label = "Projection Paint"
    bl_idname = "CAMPROJPAINT_PT_object_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'object'

    @classmethod
    def poll(cls, context):
        """Only show this panel for mesh objects"""
        return context.object and context.object.type == 'MESH'

    def get_node_display_text(self, node_name, mat_data):
        """
        Get display text for a texture node, showing label first then name in parentheses.
        
        Args:
            node_name: The texture node's name
            mat_data: The material data containing the original material name
            
        Returns:
            str: Display text in format "Label (node_name)" or just "node_name" if no label
        """
        node_label = node_name
        
        # Get the node from the original material to access its label
        original_mat = bpy.data.materials.get(mat_data.original_material_name)
        if original_mat and original_mat.use_nodes:
            node = original_mat.node_tree.nodes.get(node_name)
            if node and node.label:
                node_label = node.label
        
        # Display: "Label (node_name)" or just "node_name" if no label
        if node_label != node_name:
            return f"{node_label} ({node_name})"
        else:
            return node_name

    def draw(self, context):
        layout = self.layout
        obj = context.object
        scene = context.scene

        if not obj:
            layout.label(text="No active object")
            return

        # Ensure the property group exists on the object
        if not hasattr(obj, 'cam_proj_paint'):
            layout.label(text="Projection Paint properties not available")
            return

        # Enable toggle
        box = layout.box()
        box.label(text="Projection Paint", icon='TPAINT_HLT')
        box.prop(obj.cam_proj_paint, "enabled", text="Enabled")
        
        if not obj.cam_proj_paint.enabled:
            return
        
        # Preview Only toggle
        box.prop(obj.cam_proj_paint, "preview_only", text="Preview Only (Skip Baking)")
        
        layout.separator()
        
        # Projection Mode and UV Controls
        box = layout.box()
        box.label(text="Projection Controls", icon='MOD_UVPROJECT')
        
        row = box.row()
        row.prop(obj.cam_proj_paint, "preserve_uv", text="Preserve UV")
        
        row = box.row()
        row.prop(obj.cam_proj_paint, "projection_mode", text="Visibility Mode")
        
        layout.separator()
        
        # Projection Frame Controls
        box = layout.box()
        box.label(text="Projection Frame", icon='TIME')
        
        # Get selected enabled objects
        enabled_objects = common.get_enabled_objects(context)
        selected_enabled = [o for o in context.selected_objects if o in enabled_objects]
        
        row = box.row()
        row.prop(obj.cam_proj_paint, "use_projection_frame", text="Use Stored Frame")
        
        if obj.cam_proj_paint.use_projection_frame:
            row = box.row()
            row.prop(obj.cam_proj_paint, "projection_frame", text="Frame")
            
            if obj.cam_proj_paint.projection_frame >= 0:
                row = box.row()
                row.label(text=f"Frame: {obj.cam_proj_paint.projection_frame}", icon='INFO')
            else:
                row = box.row()
                row.label(text="No frame stored yet", icon='ERROR')
        
        # Show sync button if multiple objects selected
        if len(selected_enabled) > 1:
            row = box.row()
            row.label(text=f"{len(selected_enabled)} objects selected", icon='OBJECT_DATA')
            row = box.row()
            row.scale_y = 1.2
            row.operator("camprojpaint.sync_projection_frame", icon='COPYDOWN', text="Apply to All Selected")
        
        layout.separator()
        
        box = layout.box()
        box.label(text="PSD Layer Mapping", icon='LINKED')
        
        # Check if PSD file is selected
        if not scene.cam_proj_paint.projection_psd_file:
            box.label(text="No PSD file selected", icon='ERROR')
            return
        
        # Rediscover texture nodes button
        row = box.row()
        row.operator("camprojpaint.discover_texture_nodes", icon='VIEWZOOM', text="Rediscover Texture Nodes")
        
        # Auto-map button
        row = box.row()
        row.operator("camprojpaint.auto_map_psd_layers", icon='UV_SYNC_SELECT', text="Auto-Map by Name")
        
        # Display materials and their texture nodes
        if not obj.cam_proj_paint.material_data:
            box.label(text="No materials discovered", icon='INFO')
            return
        
        layout.separator()
        
        # Iterate through materials
        for mat_data in obj.cam_proj_paint.material_data:
            mat_box = layout.box()
            
            # Material header
            mat_name = mat_data.original_material_name or f"Material {mat_data.material_index}"
            mat_box.label(text=mat_name, icon='MATERIAL')
            
            # Check if material has texture nodes
            if not mat_data.texture_nodes:
                mat_box.label(text="  No texture nodes found", icon='INFO')
                continue
            
            layer_names = None
            psd_file_path = scene.cam_proj_paint.projection_psd_file
            if psd_file_path and os.path.exists(psd_file_path):
                layer_names = []
                psd_layers = psd_handler.get_psd_layer_list(psd_file_path)
                layer_names = [layer_name for layer_name, is_group in psd_layers if not is_group]  # Exclude groups
            
            # List each texture node with PSD layer dropdown
            for tex_idx, tex_node_data in enumerate(mat_data.texture_nodes):
                # Texture node info
                node_box = mat_box.box()
                
                # Node name and image - show label first, then name in parentheses
                row = node_box.row()
                display_text = self.get_node_display_text(tex_node_data.node_name, mat_data)
                row.label(text=display_text, icon='NODE_TEXTURE')
                
                if tex_node_data.original_texture:
                    row = node_box.row()
                    row.label(text=f"  Image: {tex_node_data.original_texture.name}", icon='IMAGE_DATA')
                
                if tex_node_data.original_uv_map:
                    row = node_box.row()
                    row.label(text=f"  UV: {tex_node_data.original_uv_map}", icon='UV')
                
                # PSD layer dropdown
                row = node_box.row()
                row.prop(tex_node_data, "psd_layer_enum", text="Layer")
                
                # Warning if PSD layer is assigned but doesn't exist
                if tex_node_data.psd_layer_name and layer_names is not None:
                    if tex_node_data.psd_layer_name not in layer_names:
                        row = node_box.row()
                        row.alert = True
                        row.label(text=f"Layer '{tex_node_data.psd_layer_name}' not found in PSD!", icon='ERROR')

                # Projection filter settings
                row = node_box.row()
                row.prop(tex_node_data, "projection_filter_type", text="Filter")
                
                # Only show color picker if filter is not 'NONE'
                if tex_node_data.projection_filter_type != 'NONE':
                    row = node_box.row()
                    row.prop(tex_node_data, "projection_filter_color", text="")
                
                # Show assigned layer if set
                # if tex_node_data.psd_layer_name and tex_node_data.psd_layer_name != 'NONE':
                #     row = node_box.row()
                #     row.label(text=f"  → Assigned: {tex_node_data.psd_layer_name}", icon='CHECKMARK')

# Registration
classes = [
    CAMPROJPAINT_TextureNodeData,  # Must be registered before MaterialData
    CAMPROJPAINT_MaterialData,  # Must be registered before ObjectSettings
    CAMPROJPAINT_SceneSettings,
    CAMPROJPAINT_ObjectSettings,
    CAMPROJPAINT_OT_install_dependencies,
    CAMPROJPAINT_OT_toggle_auto_reload,
    CAMPROJPAINT_OT_enable_visible_objects,
    CAMPROJPAINT_OT_enable_selected_objects,
    CAMPROJPAINT_OT_select_enabled_objects,
    CAMPROJPAINT_OT_discover_texture_nodes,
    CAMPROJPAINT_OT_disable_selected_objects,
    CAMPROJPAINT_OT_auto_map_psd_layers,
    CAMPROJPAINT_OT_set_project_visible,
    CAMPROJPAINT_OT_set_project_all,
    CAMPROJPAINT_OT_set_project_preserve,
    CAMPROJPAINT_OT_enable_preserve_uv,
    CAMPROJPAINT_OT_disable_preserve_uv,
    CAMPROJPAINT_OT_sync_projection_frame,
    CAMPROJPAINT_OT_setup_preview_materials,
    CAMPROJPAINT_OT_setup_bake_materials,
    CAMPROJPAINT_OT_reload_projection_image,
    CAMPROJPAINT_OT_bake_projection,
    CAMPROJPAINT_OT_bake_projection_eevee,
    CAMPROJPAINT_OT_apply_baked_result,
    CAMPROJPAINT_OT_remove_temp_materials,
    CAMPROJPAINT_OT_clear_psd_mapping,
    CAMPROJPAINT_OT_remove_temp_uvs_vcols,
    CAMPROJPAINT_PT_object_panel,
    CAMPROJPAINT_PT_main_panel,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    # Add property groups
    bpy.types.Scene.cam_proj_paint = PointerProperty(type=CAMPROJPAINT_SceneSettings)
    bpy.types.Object.cam_proj_paint = PointerProperty(type=CAMPROJPAINT_ObjectSettings)
    
    # Add handlers
    bpy.app.handlers.depsgraph_update_post.append(on_projection_psd_reload)
    bpy.app.handlers.load_post.append(on_load_post)

def unregister():
    # Stop PSD file watching if active
    psd_watcher.stop_watching()
    
    # Remove handlers
    if on_projection_psd_reload in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(on_projection_psd_reload)
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
    
    # Remove property groups
    del bpy.types.Scene.cam_proj_paint
    del bpy.types.Object.cam_proj_paint
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()

