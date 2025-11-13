"""
UV-Space EEVEE Baking Module

This module provides EEVEE-based texture baking by unfolding geometry into UV space
and rendering from an orthographic camera. Much faster than Cycles baking.
"""

import bpy
import bmesh
from mathutils import Vector, Matrix
from math import radians
import tempfile
import os
import numpy as np
from scipy import ndimage

# Import common utilities
from . import common

def remove_generate_modifiers(obj):
    """
    Remove modifiers that generate new geometry from the object.
    
    Args:
        obj: The object to clean up
    """
    # List of modifier types that typically generate new geometry and should be removed
    geometry_generating_types = {
        'ARRAY', 'BEVEL', 'BOOLEAN', 'BUILD', 'DECIMATE', 'EDGE_SPLIT', 'GEOMETRY_NODES', 'MASK', 'MESH_TO_VOLUME', 'MIRROR', 'MULTIRES', 'REMESH', 'SCREW', 'SKIN', 'SOLIDIFY', 'SUBSURF', 'TRIANGULATE', 'VOLUME_TO_MESH', 'WELD', 'WIREFRAME'
    }
    
    modifiers_to_remove = [
        mod for mod in obj.modifiers 
        if mod.type in geometry_generating_types
    ]
    
    for mod in modifiers_to_remove:
        obj.modifiers.remove(mod)
    print(f"Removed {len(modifiers_to_remove)} geometry-generating modifiers from '{obj.name}'.")

def duplicate_object_for_baking(obj, collection_name=None):
    """
    Duplicate an object and its mesh data for UV baking.
    
    Args:
        obj: The object to duplicate
        collection_name: Name of temporary collection to store duplicates
        
    Returns:
        The duplicated object
    """
    if collection_name is None:
        collection_name = common.UV_BAKE_TEMP_COLLECTION
    
    # Create or get temporary collection
    temp_collection = common.get_or_create_collection(collection_name, link_to_scene=True)
    if not temp_collection:
        return None
    
    # Duplicate object and mesh data
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.duplicate()
    new_obj = bpy.context.view_layer.objects.active
    new_obj.name = f"{obj.name}_UV_Bake_Temp"
    
    
    bpy.ops.object.convert(target='MESH')
    new_obj.parent = None
    new_obj.matrix_world = Matrix.Identity(4)
    
    # Link to temporary collection
    temp_collection.objects.link(new_obj)
    
    print(f"  Created duplicate: '{new_obj.name}'")
    
    return new_obj


def unfold_mesh_to_uv_space(obj, uv_map_name, island_margin=0.002):
    """
    Unfold mesh geometry into UV space by setting vertex positions to their UV coordinates.
    Handles UV seams by splitting vertices that have different UV coordinates per face.
    
    Args:
        obj: The mesh object to unfold
        uv_map_name: Name of the UV map to use for unfolding
        island_margin: Scale factor for UV islands to close seam gaps (default: 0.002 = 0.2%)
        
    Returns:
        True if successful, False otherwise
    """
    if obj.type != 'MESH':
        print(f"  Error: Object '{obj.name}' is not a mesh")
        return False
    
    mesh = obj.data
    
    # Check if UV map exists
    if uv_map_name not in mesh.uv_layers:
        print(f"  Error: UV map '{uv_map_name}' not found in '{obj.name}'")
        return False
    
    # Create bmesh from mesh
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    
    # Get UV layer
    uv_layer = bm.loops.layers.uv.get(uv_map_name)
    if not uv_layer:
        print(f"  Error: Could not access UV layer '{uv_map_name}'")
        bm.free()
        return False
    
    # Build mapping of (vertex, face) -> UV coordinate
    # This is necessary because a vertex can have different UVs on different faces (UV seams)
    vert_face_uv = {}
    
    for face in bm.faces:
        for loop in face.loops:
            vert = loop.vert
            uv = loop[uv_layer].uv
            key = (vert.index, face.index)
            vert_face_uv[key] = uv.copy()
    
    # Split vertices at UV seams
    # For each vertex, check if it has multiple different UV coordinates
    vert_uvs = {}  # vert.index -> list of unique UV coords
    
    for (vert_idx, face_idx), uv in vert_face_uv.items():
        if vert_idx not in vert_uvs:
            vert_uvs[vert_idx] = []
        
        # Check if this UV is unique (not already in list)
        is_unique = True
        for existing_uv in vert_uvs[vert_idx]:
            if (existing_uv - uv).length < 0.0001:  # Small epsilon for floating point comparison
                is_unique = False
                break
        
        if is_unique:
            vert_uvs[vert_idx].append(uv)
    
    # Split vertices that have multiple UV coordinates
    verts_to_split = [idx for idx, uvs in vert_uvs.items() if len(uvs) > 1]
    
    if verts_to_split:
        print(f"  Splitting {len(verts_to_split)} vertices at UV seams")
        
        # Use bmesh split_edges to separate faces at UV boundaries
        # This is more reliable than manually duplicating vertices
        edges_to_split = set()
        
        for vert_idx in verts_to_split:
            vert = bm.verts[vert_idx]
            # Find edges connected to this vertex where adjacent faces have different UVs
            for edge in vert.link_edges:
                if len(edge.link_faces) == 2:
                    face1, face2 = edge.link_faces
                    
                    # Get UV coords for this vertex on both faces
                    uv1 = None
                    uv2 = None
                    
                    for loop in face1.loops:
                        if loop.vert == vert:
                            uv1 = loop[uv_layer].uv
                            break
                    
                    for loop in face2.loops:
                        if loop.vert == vert:
                            uv2 = loop[uv_layer].uv
                            break
                    
                    # If UVs differ, mark edge for splitting
                    if uv1 and uv2 and (uv1 - uv2).length > 0.0001:
                        edges_to_split.add(edge)
        
        if edges_to_split:
            bmesh.ops.split_edges(bm, edges=list(edges_to_split))
            print(f"  Split {len(edges_to_split)} edges at UV seams")
    
    # Now set vertex positions to UV coordinates
    # After splitting, each vertex should have a consistent UV coordinate across its faces
    cam_offset = Vector((.5, .5, 0))
    for vert in bm.verts:
        # Get UV coordinate from first face this vertex belongs to
        # (should be consistent now after splitting)
        if vert.link_faces:
            first_face = vert.link_faces[0]
            for loop in first_face.loops:
                if loop.vert == vert:
                    uv = loop[uv_layer].uv
                    # Set vertex position to (u, v, 0) in world space with offset
                    vert.co = Vector((uv.x, uv.y, 0)) - cam_offset
                    break
    
    # Scale UV islands outward to close seam gaps
    # This helps prevent visible gaps at UV seams after rendering
    if island_margin > 0:
        print(f"  Scaling UV islands by {island_margin*100:.2f}% to close seam gaps...")
        
        # Find UV islands (connected face groups)
        # We'll use face connectivity to identify islands
        face_islands = []
        unprocessed_faces = set(bm.faces)
        
        while unprocessed_faces:
            # Start a new island
            island = set()
            to_process = {unprocessed_faces.pop()}
            
            while to_process:
                face = to_process.pop()
                if face in island:
                    continue
                
                island.add(face)
                
                # Add connected faces (faces sharing vertices)
                for vert in face.verts:
                    for linked_face in vert.link_faces:
                        if linked_face in unprocessed_faces:
                            to_process.add(linked_face)
                            unprocessed_faces.discard(linked_face)
            
            face_islands.append(island)
        
        print(f"  Found {len(face_islands)} UV island(s)")
        
        # Scale each island from its center
        for island in face_islands:
            if not island:
                continue
            
            # Calculate island center (average of all vertex positions)
            island_verts = set()
            for face in island:
                island_verts.update(face.verts)
            
            if not island_verts:
                continue
            
            center = Vector((0, 0, 0))
            for vert in island_verts:
                center += vert.co
            center /= len(island_verts)
            
            # Scale vertices away from center
            scale_factor = 1.0 + island_margin
            for vert in island_verts:
                # Move vertex away from island center
                direction = vert.co - center
                vert.co = center + direction * scale_factor
    
    # Apply changes back to mesh
    bm.to_mesh(mesh)
    bm.free()
    
    # Clear parent and reset transform
    # This ensures the object is positioned exactly at its vertex coordinates
    obj.parent = None
    # Set object to camera center
    obj.matrix_world = Matrix.Identity(4)
    obj.location = cam_offset
    
    mesh.update()
    
    print(f"  Unfolded mesh to UV space using '{uv_map_name}'")
    
    return True


def prepare_object_for_uv_bake(obj, uv_map_name, temp_collection_name=None, island_margin=0.002):
    """
    Complete Phase 1: Duplicate object and unfold to UV space.
    
    Args:
        obj: Original object to bake
        uv_map_name: UV map to use for baking
        temp_collection_name: Name of temporary collection
        island_margin: Scale factor for UV islands to close seam gaps
        
    Returns:
        The prepared duplicate object, or None if failed
    """
    if temp_collection_name is None:
        temp_collection_name = common.UV_BAKE_TEMP_COLLECTION
    
    print(f"\nPreparing '{obj.name}' for UV baking:")
    
    # Step 1A: Duplicate object
    duplicate = duplicate_object_for_baking(obj, temp_collection_name)
    if not duplicate:
        return None
    
    # Step 1B: Unfold to UV space
    if not unfold_mesh_to_uv_space(duplicate, uv_map_name, island_margin):
        # Cleanup on failure
        common.remove_object(duplicate)
        return None
    
    # Step 1C: Materials are already applied from duplication
    # The duplicate has the same material slots as the original
    print(f"  Materials ready: {len(duplicate.data.materials)} material(s)")
    
    return duplicate


# ============================================================================
# Phase 2: Camera Setup
# ============================================================================

def create_uv_bake_camera(collection_name=None):
    """
    Step 2A: Create orthographic camera for UV space rendering.
    
    The camera is positioned to look down at the 0-1 UV space from above.
    
    Args:
        collection_name: Name of temporary collection to store camera
        
    Returns:
        The created camera object
    """
    if collection_name is None:
        collection_name = common.UV_BAKE_TEMP_COLLECTION
    
    # Check if camera already exists
    existing_camera = bpy.data.objects.get(common.UV_BAKE_CAMERA_NAME)
    if existing_camera:
        print(f"  Using existing {common.UV_BAKE_CAMERA_NAME}")
        return existing_camera
    
    # Create camera data
    camera_data = bpy.data.cameras.new(name=common.UV_BAKE_CAMERA_NAME)
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = 1.0  # Matches 0-1 UV range
    
    # Create camera object
    camera_obj = bpy.data.objects.new(common.UV_BAKE_CAMERA_NAME, camera_data)
    
    # Position at center of UV space, looking down
    camera_obj.location = (0.5, 0.5, 1.0)
    
    # Rotate to point straight down
    camera_obj.rotation_euler = (0, 0, 0)
    
    # Get or create temporary collection
    temp_collection = common.get_or_create_collection(collection_name, link_to_scene=True)
    if not temp_collection:
        return None
    
    # Link camera to collection
    temp_collection.objects.link(camera_obj)
    
    print(f"  Created {common.UV_BAKE_CAMERA_NAME} at (0.5, 0.5, 1.0)")
    
    return camera_obj


def scale_object_to_fill_camera(obj, resolution_x, resolution_y):
    """
    Step 2B: Scale UV-unfolded object to match camera aspect ratio.
    
    This ensures the baked texture matches the desired resolution's aspect ratio.
    The object is scaled to compensate for non-square resolutions.
    
    Args:
        obj: The UV-unfolded object to scale
        resolution_x: Target texture width
        resolution_y: Target texture height
    """
    # Set object's origin to center of UV space (camera position)
    # This is where the object should be centered for proper scaling
    obj.location = (0.5, 0.5, 0.0)
    
    # Calculate aspect ratio scaling
    if resolution_x > resolution_y:
        # Wider than tall - compress Y
        scale_x = 1.0
        scale_y = resolution_y / resolution_x
        print(f"  Scaling object: X={scale_x:.3f}, Y={scale_y:.3f} (wider texture)")
    elif resolution_y > resolution_x:
        # Taller than wide - compress X
        scale_x = resolution_x / resolution_y
        scale_y = 1.0
        print(f"  Scaling object: X={scale_x:.3f}, Y={scale_y:.3f} (taller texture)")
    else:
        # Square - no scaling needed
        scale_x = 1.0
        scale_y = 1.0
        print(f"  Scaling object: X={scale_x:.3f}, Y={scale_y:.3f} (square texture)")
    
    # Apply scale
    obj.scale = (scale_x, scale_y, 1.0)


def setup_uv_bake_camera(obj, resolution_x=2048, resolution_y=2048):
    """
    Complete Phase 2: Setup camera and scale object to fill frame.
    
    Args:
        obj: The UV-unfolded object to render
        resolution_x: Target texture width (default: 2048)
        resolution_y: Target texture height (default: 2048)
        
    Returns:
        The UV bake camera object
    """
    print(f"\nSetting up UV bake camera (Resolution: {resolution_x}x{resolution_y}):")
    
    # Step 2A: Create camera
    camera = create_uv_bake_camera()
    
    # Step 2B: Scale object to fill camera based on aspect ratio
    scale_object_to_fill_camera(obj, resolution_x, resolution_y)
    
    return camera


# ============================================================================
# Phase 3: EEVEE Rendering
# ============================================================================

def dilate_image_margins(image, iterations=8):
    """
    Dilate non-transparent pixels outward to fill margin gaps and prevent seams.
    
    This uses iterative dilation to expand the color data from visible pixels
    into the transparent border areas around UV islands. This prevents seams
    from appearing at UV boundaries during texture sampling.
    
    Args:
        image: Blender image datablock to dilate
        iterations: Number of dilation passes (controls margin width in pixels)
    
    Returns:
        True if successful, False otherwise
    """
    if not image or not image.pixels:
        return False
    
    width = image.size[0]
    height = image.size[1]
    
    # Get pixel data as numpy array (RGBA, flattened)
    pixels = np.array(image.pixels[:]).reshape((height, width, 4))
    
    # Create alpha mask (where we have content)
    alpha_mask = pixels[:, :, 3] > 0.01
    
    # For each RGB channel, dilate using the alpha mask
    for channel in range(3):
        channel_data = pixels[:, :, channel].copy()
        current_mask = alpha_mask.copy()
        
        # Iteratively dilate
        for i in range(iterations):
            # Dilate the mask by one pixel
            dilated_mask = ndimage.binary_dilation(current_mask)
            
            # Find newly exposed pixels (dilated area minus current area)
            new_pixels = dilated_mask & ~current_mask
            
            if not np.any(new_pixels):
                # No more pixels to dilate
                break
            
            # Fill new pixels with average of neighboring filled pixels
            # Use a simple averaging filter on the current channel data
            averaged = ndimage.uniform_filter(channel_data, size=3, mode='constant', cval=0.0)
            
            # Only update the newly dilated pixels
            channel_data[new_pixels] = averaged[new_pixels]
            
            # Update mask for next iteration
            current_mask = dilated_mask
        
        # Write dilated channel back
        pixels[:, :, channel] = channel_data
    
    # Set alpha to 1.0 where we dilated (optional - keeps transparent areas transparent)
    # pixels[:, :, 3] = np.where(current_mask, 1.0, pixels[:, :, 3])
    
    # Write back to image
    image.pixels[:] = pixels.flatten()
    image.update()
    
    return True


def configure_render_settings(scene, resolution_x, resolution_y, use_transparent=True):
    """
    Step 3A: Configure render settings for EEVEE baking.
    
    Args:
        scene: The scene to configure
        resolution_x: Target texture width
        resolution_y: Target texture height
        use_transparent: Enable transparent film background
        
    Returns:
        RenderSettings object with stored original settings
    """
    print(f"\nConfiguring render settings:")
    
    # Store original settings
    original_settings = common.RenderSettings()
    original_settings.store(scene)
    
    # Set render engine to EEVEE
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    print(f"  Render engine: EEVEE Next")
    
    # Set resolution
    scene.render.resolution_x = resolution_x
    scene.render.resolution_y = resolution_y
    print(f"  Resolution: {resolution_x} x {resolution_y}")
    
    # Disable color management - set to Standard (no color transformation)
    scene.view_settings.view_transform = 'Standard'
    print(f"  View Transform: Standard (no color management)")
    
    # Set transparent background
    scene.render.film_transparent = use_transparent
    print(f"  Film Transparent: {use_transparent}")
    
    # Set samples (1 is enough for clean baking without AA)
    scene.eevee.taa_render_samples = 1
    print(f"  EEVEE Samples: 1")
    
    return original_settings


def separate_object_by_materials(obj, collection_name=None):
    if collection_name is None:
        collection_name = common.UV_BAKE_TEMP_COLLECTION
    
    if obj.type == 'MESH':
        # Separate by material
        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.separate(type='MATERIAL')
        bpy.ops.object.mode_set(mode='OBJECT')
        separated = [bpy.data.objects[o.name] for o in bpy.context.selected_objects]
        
        # Create or get temporary collection
        coll_target = common.get_or_create_collection(collection_name, link_to_scene=True)
        if not coll_target:
            print(f"  Failed to get/create collection '{collection_name}'")
            return separated

        # Change separated objects' collection
        if coll_target:
            for obj_sep in separated:
                if obj_sep.name not in coll_target.objects:
                    coll_target.objects.link(obj_sep)
                for coll in list(obj_sep.users_collection):
                    if coll.name != collection_name and obj_sep.name in coll.objects:
                        coll.objects.unlink(obj_sep)
                        
        print(f"  Separated into {len(separated)} object(s) by materials")
        return separated
    else:
        print(f"Object '{obj.name}' is not a mesh.")
        return []

def render_to_image(scene, camera, target_image, render_object=None, margin_pixels=8):
    """
    Step 3B & 3C: Render scene and copy result to target image.
    
    Args:
        scene: The scene to render
        camera: The camera to render from
        target_image: The image to copy render result into
        render_object: Optional - the specific object to render. If provided, all other
                      renderable objects will be temporarily hidden
        margin_pixels: Number of pixels to dilate for UV margins (prevents seams)
        
    Returns:
        True if successful, False otherwise
    """
    # Set active camera
    scene.camera = camera
    
    # Store original hide_render states if we need to hide other objects
    original_hide_states = {}
    
    if render_object:
        # Hide all other renderable objects (meshes, curves, etc.)
        renderable_types = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME'}
        
        for obj in scene.objects:
            if obj.type in renderable_types and obj != render_object:
                original_hide_states[obj] = obj.hide_render
                obj.hide_render = True
    
    try:
        # Render
        print(f"  Rendering...")
        bpy.ops.render.render()
        
        # Get render result using next() to find RENDER_RESULT type
        render_result = next((img for img in bpy.data.images if img.type == 'RENDER_RESULT'), None)
        if not render_result:
            print(f"  Error: Render result not found")
            return False
        
        # Save render result to temporary file (required to access pixels)
        temp_dir = tempfile.gettempdir()
        temp_filepath = os.path.join(temp_dir, common.UV_BAKE_TEMP_RENDER_FILENAME)
        
        print(f"  Saving render to temp file...")
        render_result.save_render(filepath=temp_filepath)
        
        # Load the saved render
        temp_loaded = bpy.data.images.load(temp_filepath)
        
        # Ensure target image has correct size and copy pixels
        if not common.copy_image_pixels(temp_loaded, target_image, resize_if_needed=True):
            print(f"  Error: Failed to copy pixels to target image")
            common.remove_image(temp_loaded)
            return False
        
        # Dilate margins to prevent seams at UV boundaries
        if margin_pixels > 0:
            print(f"  Dilating margins ({margin_pixels} pixels)...")
            if dilate_image_margins(target_image, iterations=margin_pixels):
                print(f"  ✓ Margins dilated successfully")
            else:
                print(f"  ⚠ Warning: Margin dilation failed")
        
        # Cleanup temporary files
        common.remove_image(temp_loaded)
        try:
            os.remove(temp_filepath)
        except:
            pass  # Ignore if file can't be deleted
        
        print(f"  Copied render result to '{target_image.name}'")
        
        return True
    
    finally:
        # Restore original hide_render states
        for obj, hide_state in original_hide_states.items():
            obj.hide_render = hide_state


def render_uv_bake(obj, camera, resolution_x=2048, resolution_y=2048, use_transparent=True, margin_pixels=8):
    """
    Complete Phase 3: Render UV-baked textures for each material.
    
    This function separates the object by materials and renders each material separately.
    This ensures clean renders without bleed-through between materials.
    
    Args:
        obj: The UV-unfolded object to render
        camera: The UV bake camera
        resolution_x: Target texture width
        resolution_y: Target texture height
        use_transparent: Enable transparent background
        margin_pixels: Number of pixels to dilate for UV margins (prevents seams)
        
    Returns:
        Dictionary of {material_index: rendered_image}
    """
    scene = bpy.context.scene
    
    print("\n" + "="*60)
    print("EEVEE UV BAKE - PHASE 3: RENDER")
    print("="*60)
    
    # Step 3A: Configure render settings
    original_settings = configure_render_settings(scene, resolution_x, resolution_y, use_transparent)
    
    # Set the active camera
    scene.camera = camera
    
    rendered_images = {}
    separated_objects = []
    
    try:
        # Step 3B: Separate object by materials
        # Record original material indexes mat_name -> mat_index
        og_mat_indexes = {mat.name: i for i, mat in enumerate(obj.data.materials)}
        print(f"\nSeparating object '{obj.name}' by materials...")
        separated_objects = separate_object_by_materials(obj)
        if not separated_objects:
            print("  No separated objects created - object may have no materials")
            return {}
        
        print(f"\nRendering {len(separated_objects)} separated object(s):")
        # print names of separated objects and the materials they correspond to
        for sep_obj in separated_objects:
            mat_index = sep_obj.active_material_index if sep_obj.active_material_index < len(sep_obj.data.materials) else -1
            mat_name = sep_obj.data.materials[mat_index].name if mat_index != -1 else "None"
            print(f"  - '{sep_obj.name}': Material Index {mat_index}, Material Name '{mat_name}'")
        # Step 3C: Render each separated object
        for separated_obj in separated_objects:
            material = separated_obj.data.materials[0] if separated_obj.data.materials else None
            if not material:
                print(f"  Object '{separated_obj.name}': No material - skipping")
                continue
            
            # Find the material index in the original object
            mat_index = og_mat_indexes.get(material.name, -1)
            if mat_index == -1:
                print(f"  Object '{separated_obj.name}': Material '{material.name}' not found - skipping")
                continue
            
            print(f"\n  Material {mat_index}: '{material.name}' (object: '{separated_obj.name}')")
            
            # Create temporary image for this material
            temp_image = common.create_image(
                name=f"{common.TEMP_BAKE_IMAGE_PREFIX}{separated_obj.name}_{mat_index}",
                width=resolution_x,
                height=resolution_y,
                alpha=use_transparent
            )
            if not temp_image:
                print(f"  ✗ Material {mat_index} failed to create temp image")
                continue
            
            # Render and copy to image (with margin dilation)
            if render_to_image(scene, camera, temp_image, render_object=separated_obj, margin_pixels=margin_pixels):
                rendered_images[mat_index] = temp_image
                print(f"  ✓ Material {mat_index} rendered successfully")
            else:
                print(f"  ✗ Material {mat_index} render failed")
                common.remove_image(temp_image)
        
        print("\n" + "="*60)
        print(f"✓ Rendered {len(rendered_images)}/{len(separated_objects)} materials")
        print("="*60 + "\n")
    finally:
        # Always restore original settings
        original_settings.restore(scene)
        
        # # Cleanup separated objects
        print("Cleaning up separated objects...")
        for separated_obj in separated_objects:
            common.remove_object(separated_obj)

    return rendered_images


# ============================================================================
# UI Panel for Testing
# ============================================================================

class UVBAKE_PT_test_panel(bpy.types.Panel):
    """Test panel for EEVEE UV Baking"""
    bl_label = "EEVEE UV Bake (Test)"
    bl_idname = "UVBAKE_PT_test_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Projection Paint'
    
    @classmethod
    def poll(cls, context):
        # Only show panel if enabled in addon preferences
        preferences = context.preferences.addons.get(__package__)
        if preferences and hasattr(preferences.preferences, 'show_test_ui'):
            return preferences.preferences.show_test_ui
        return False
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        # Phase 1 box
        box = layout.box()
        box.label(text="Phase 1: UV Unfolding", icon='UV')
        
        # Active object info
        obj = context.active_object
        if obj and obj.type == 'MESH':
            box.label(text=f"Active: {obj.name}", icon='OBJECT_DATA')
            
            # UV map info
            if obj.data.uv_layers:
                uv_map = obj.data.uv_layers.active
                if uv_map:
                    box.label(text=f"UV Map: {uv_map.name}", icon='UV_DATA')
            else:
                box.label(text="No UV maps!", icon='ERROR')
        else:
            box.label(text="No mesh selected", icon='ERROR')
        
        # Island margin option
        row = box.row()
        row.prop(scene, "uv_bake_island_margin", text="Island Margin", slider=True)
        
        box.separator()
        
        # Main test button
        row = box.row()
        row.scale_y = 2.0
        op = row.operator("uvbake.prepare_object", icon='MOD_UVPROJECT', text="Step 1: Prepare for Bake")
        # Ensure a strict boolean is assigned (avoid None from short-circuiting)
        row.enabled = (obj is not None and obj.type == 'MESH' and len(obj.data.uv_layers) > 0)

        box.separator()
        box.label(text="Creates UV-unfolded duplicate", icon='INFO')
        
        layout.separator()
        
        # Phase 2 box
        box = layout.box()
        box.label(text="Phase 2: Camera Setup", icon='CAMERA_DATA')
        
        # Resolution settings
        row = box.row(align=True)
        row.label(text="Resolution:")
        row.prop(scene, "uv_bake_res_x", text="X")
        row.prop(scene, "uv_bake_res_y", text="Y")
        
        box.separator()
        
        # Phase 2 button
        row = box.row()
        row.scale_y = 2.0
        row.operator("uvbake.setup_camera", icon='CAMERA_DATA', text="Step 2: Setup Camera")
        # Make sure row.enabled is a boolean (obj could be None)
        row.enabled = (obj is not None and obj.type == 'MESH')

        box.separator()
        box.label(text="Creates ortho camera + scales object", icon='INFO')
        
        layout.separator()
        
        # Phase 3 box
        box = layout.box()
        box.label(text="Phase 3: Render", icon='RENDER_STILL')
        
        # Transparent background option
        row = box.row()
        row.prop(scene, "uv_bake_transparent", text="Transparent Background")
        
        # Margin pixels option
        row = box.row()
        row.prop(scene, "uv_bake_margin", text="Margin (pixels)")
        
        box.separator()
        
        # Phase 3 button
        row = box.row()
        row.scale_y = 2.0
        row.operator("uvbake.render", icon='RENDER_STILL', text="Step 3: Render Bake")
        # Ensure boolean value for enabled
        row.enabled = (obj is not None and obj.type == 'MESH')

        box.separator()
        box.label(text="Renders each material to image", icon='INFO')
        
        layout.separator()
        
        # Cleanup section
        box = layout.box()
        box.label(text="Cleanup", icon='TRASH')
        row = box.row()
        row.operator("uvbake.cleanup_temp", icon='TRASH', text="Cleanup Temp Objects")
        
        # Info
        box.separator()
        box.label(text="Creates UV-unfolded duplicate", icon='INFO')


# ============================================================================
# Operators
# ============================================================================

class UVBAKE_OT_prepare_object(bpy.types.Operator):
    """Prepare object for UV baking (Steps 1A, 1B, 1C)"""
    bl_idname = "uvbake.prepare_object"
    bl_label = "Prepare Object for UV Bake"
    bl_description = "Duplicate object and unfold to UV space"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.active_object
        
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object")
            return {'CANCELLED'}
        
        # Get active UV map
        if not obj.data.uv_layers:
            self.report({'ERROR'}, "Object has no UV maps")
            return {'CANCELLED'}
        
        uv_map_name = obj.data.uv_layers.active.name
        scene = context.scene
        island_margin = scene.uv_bake_island_margin
        
        print("\n" + "="*60)
        print("EEVEE UV BAKE - PHASE 1: PREPARE OBJECT")
        print("="*60)
        
        # Execute Phase 1: Steps 1A, 1B, 1C
        duplicate = prepare_object_for_uv_bake(obj, uv_map_name, island_margin=island_margin)
        
        if duplicate:
            print("="*60)
            print(f"✓ SUCCESS: Created '{duplicate.name}'")
            print("="*60 + "\n")
            
            # Select the duplicate to show result
            bpy.ops.object.select_all(action='DESELECT')
            duplicate.select_set(True)
            context.view_layer.objects.active = duplicate
            
            self.report({'INFO'}, f"Created UV-unfolded duplicate: {duplicate.name}")
        else:
            print("="*60)
            print("✗ FAILED: Could not prepare object")
            print("="*60 + "\n")
            
            self.report({'ERROR'}, "Failed to prepare object for UV baking")
            return {'CANCELLED'}
        
        return {'FINISHED'}


class UVBAKE_OT_setup_camera(bpy.types.Operator):
    """Setup UV bake camera (Steps 2A, 2B)"""
    bl_idname = "uvbake.setup_camera"
    bl_label = "Setup UV Bake Camera"
    bl_description = "Create orthographic camera and scale object to fill frame"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.active_object
        
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object")
            return {'CANCELLED'}
        
        scene = context.scene
        resolution_x = scene.uv_bake_res_x
        resolution_y = scene.uv_bake_res_y
        
        print("\n" + "="*60)
        print("EEVEE UV BAKE - PHASE 2: SETUP CAMERA")
        print("="*60)
        
        # Execute Phase 2: Steps 2A, 2B
        camera = setup_uv_bake_camera(obj, resolution_x, resolution_y)
        
        if camera:
            print("="*60)
            print(f"✓ SUCCESS: Camera ready, object scaled")
            print("="*60 + "\n")
            
            # Set as active camera for preview
            context.scene.camera = camera
            
            self.report({'INFO'}, f"Created UV bake camera ({resolution_x}x{resolution_y})")
        else:
            print("="*60)
            print("✗ FAILED: Could not setup camera")
            print("="*60 + "\n")
            
            self.report({'ERROR'}, "Failed to setup UV bake camera")
            return {'CANCELLED'}
        
        return {'FINISHED'}


class UVBAKE_OT_render(bpy.types.Operator):
    """Render UV bake using EEVEE (Step 3)"""
    bl_idname = "uvbake.render"
    bl_label = "Render UV Bake"
    bl_description = "Render each material separately using EEVEE"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.active_object
        
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object")
            return {'CANCELLED'}
        
        # Check if UV_Bake_Camera exists
        camera = bpy.data.objects.get(common.UV_BAKE_CAMERA_NAME)
        if not camera:
            self.report({'ERROR'}, f"{common.UV_BAKE_CAMERA_NAME} not found. Run Step 2 first")
            return {'CANCELLED'}
        
        scene = context.scene
        resolution_x = scene.uv_bake_res_x
        resolution_y = scene.uv_bake_res_y
        use_transparent = scene.uv_bake_transparent
        margin_pixels = scene.uv_bake_margin
        
        # Execute Phase 3
        rendered_images = render_uv_bake(
            obj, 
            camera, 
            resolution_x, 
            resolution_y, 
            use_transparent,
            margin_pixels
        )
        
        if rendered_images:
            # Show first rendered image in UV editor for preview
            if len(rendered_images) > 0:
                first_image = list(rendered_images.values())[0]
                # Try to show in image editor
                for area in context.screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        area.spaces.active.image = first_image
                        break
            
            self.report({'INFO'}, f"Rendered {len(rendered_images)} material(s)")
        else:
            self.report({'WARNING'}, "No materials were rendered")
            return {'CANCELLED'}
        
        return {'FINISHED'}


class UVBAKE_OT_cleanup_temp(bpy.types.Operator):
    """Cleanup temporary UV bake objects and collections"""
    bl_idname = "uvbake.cleanup_temp"
    bl_label = "Cleanup Temporary Objects"
    bl_description = "Remove all temporary UV bake objects and collections"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        removed_objects = 0
        removed_images = 0
        
        # Find and remove temporary collection
        temp_collection = bpy.data.collections.get(common.UV_BAKE_TEMP_COLLECTION)
        if temp_collection:
            # Remove all objects in the collection
            for obj in list(temp_collection.objects):
                common.remove_object(obj)
                removed_objects += 1
            
            # Remove the collection itself
            common.remove_collection(temp_collection)
        
        # Remove rendered images
        for img in list(bpy.data.images):
            if img.name.startswith("UV_Bake_Render_"):
                common.remove_image(img)
                removed_images += 1
        
        if removed_objects > 0 or removed_images > 0:
            print(f"\nCleaned up {removed_objects} objects and {removed_images} images")
            self.report({'INFO'}, f"Removed {removed_objects} objects, {removed_images} images")
        else:
            self.report({'INFO'}, "No temporary data to clean up")
        
        return {'FINISHED'}


# Test/Debug operator (kept for backwards compatibility)
class UVBAKE_OT_test_unfold(bpy.types.Operator):
    """Test UV unfolding on selected object"""
    bl_idname = "uvbake.test_unfold"
    bl_label = "Test UV Unfold"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.active_object
        
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object")
            return {'CANCELLED'}
        
        # Get first UV map
        if not obj.data.uv_layers:
            self.report({'ERROR'}, "Object has no UV maps")
            return {'CANCELLED'}
        
        uv_map_name = obj.data.uv_layers[0].name
        
        # Prepare for baking
        duplicate = prepare_object_for_uv_bake(obj, uv_map_name)
        
        if duplicate:
            self.report({'INFO'}, f"Created UV-unfolded duplicate: {duplicate.name}")
        else:
            self.report({'ERROR'}, "Failed to prepare object for UV baking")
            return {'CANCELLED'}
        
        return {'FINISHED'}


# ============================================================================
# Addon Preferences
# ============================================================================

class UVBAKE_AddonPreferences(bpy.types.AddonPreferences):
    """Addon preferences for UV Bake module"""
    bl_idname = __package__
    
    show_test_ui: bpy.props.BoolProperty(
        name="Show Test UI Panel",
        description="Enable the EEVEE UV Bake test panel in the 3D viewport sidebar",
        default=False
    )
    
    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="UV Bake Settings:", icon='UV')
        box.prop(self, "show_test_ui")


# ============================================================================
# Scene Properties
# ============================================================================

def register_properties():
    """Register scene properties for UV baking settings"""
    bpy.types.Scene.uv_bake_res_x = bpy.props.IntProperty(
        name="Resolution X",
        description="Bake texture width",
        default=2048,
        min=64,
        max=8192
    )
    
    bpy.types.Scene.uv_bake_res_y = bpy.props.IntProperty(
        name="Resolution Y",
        description="Bake texture height",
        default=2048,
        min=64,
        max=8192
    )
    
    bpy.types.Scene.uv_bake_transparent = bpy.props.BoolProperty(
        name="Transparent Background",
        description="Use transparent film background for rendering",
        default=True
    )
    
    bpy.types.Scene.uv_bake_margin = bpy.props.IntProperty(
        name="Margin",
        description="Number of pixels to dilate around UV islands to prevent seams",
        default=8,
        min=0,
        max=64
    )
    
    bpy.types.Scene.uv_bake_island_margin = bpy.props.FloatProperty(
        name="Island Margin",
        description="Scale factor for UV islands to close seam gaps (0.002 = 0.2% larger)",
        default=0.002,
        min=0.0,
        max=0.02,
        precision=4,
        step=0.01
    )


def unregister_properties():
    """Unregister scene properties"""
    del bpy.types.Scene.uv_bake_island_margin
    del bpy.types.Scene.uv_bake_margin
    del bpy.types.Scene.uv_bake_transparent
    del bpy.types.Scene.uv_bake_res_y
    del bpy.types.Scene.uv_bake_res_x


# ============================================================================
# Registration
# ============================================================================

def register():
    register_properties()
    bpy.utils.register_class(UVBAKE_AddonPreferences)
    bpy.utils.register_class(UVBAKE_PT_test_panel)
    bpy.utils.register_class(UVBAKE_OT_prepare_object)
    bpy.utils.register_class(UVBAKE_OT_setup_camera)
    bpy.utils.register_class(UVBAKE_OT_render)
    bpy.utils.register_class(UVBAKE_OT_cleanup_temp)
    bpy.utils.register_class(UVBAKE_OT_test_unfold)


def unregister():
    bpy.utils.unregister_class(UVBAKE_OT_test_unfold)
    bpy.utils.unregister_class(UVBAKE_OT_cleanup_temp)
    bpy.utils.unregister_class(UVBAKE_OT_render)
    bpy.utils.unregister_class(UVBAKE_OT_setup_camera)
    bpy.utils.unregister_class(UVBAKE_OT_prepare_object)
    bpy.utils.unregister_class(UVBAKE_PT_test_panel)
    bpy.utils.unregister_class(UVBAKE_AddonPreferences)
    unregister_properties()


if __name__ == "__main__":
    register()
