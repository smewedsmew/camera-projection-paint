"""
Common utilities for Camera Projection Paint addon.

This module provides shared utility functions for image operations,
object management, and other common tasks.
"""

import bpy
import numpy

# =============================================================================
# Constants
# =============================================================================

# Prefix for original texture backups
ORIGINAL_TEXTURE_PREFIX = "OG_"

# Suffixes used for temporary bake uv/materials/images
PROJECTION_UV_NAME = "CPP_Projection"
PROJECTION_VIS_VCOL_NAME = "CPP_Projection_Visibility"
PREVIEW_MAT_SUFFIX = "_CPPPreview"
BAKE_MAT_SUFFIX = "_CPPBake"
BAKE_TARGET_IMG_SUFFIX = "_CPPBakeTarget"

# Collection name for temporary UV baking objects
UV_BAKE_TEMP_COLLECTION = "UV_Bake_Temp"

# UV baking camera name
UV_BAKE_CAMERA_NAME = "UV_Bake_Camera"

# Temporary bake image prefix
TEMP_BAKE_IMAGE_PREFIX = "TMP_BAKE_"

# Temporary render filename for UV baking
UV_BAKE_TEMP_RENDER_FILENAME = "uv_bake_temp_render.png"

# Node names for material setup
NODE_NAME_PROJECTION_UV = "Projection_UV"
NODE_NAME_PROJECTION_TEXTURE = "Projection_Texture"
NODE_NAME_PROJECTION_FILTER = "Projection_Filter"
NODE_NAME_PROJECTION_VISIBILITY = "Projection_Visibility_Node"
NODE_NAME_PROJECTION_VISIBILITY_ADD = "Projection_Visibility_Add"
NODE_NAME_PROJECTION_ALPHA_MULTIPLY = "Projection_Alpha_Multiply"
NODE_NAME_PROJECTION_MIX = "Projection_Mix"
NODE_NAME_ORIGINAL_TEXTURE = "Original_Texture"
NODE_NAME_ORIGINAL_UV = "Original_UV"
NODE_NAME_BAKE_PRINCIPLED = "Bake_Principled"
NODE_NAME_BAKE_TARGET = "Bake_Target"

# =============================================================================
# String Utilities
# =============================================================================

def node_name_to_label(node_name):
    """
    Convert a node name to a human-readable label.
    
    Args:
        node_name: Node name with underscores
    
    Returns:
        Label with spaces instead of underscores
    """
    return node_name.replace('_', ' ')

def parse_comma_separated_list(text, lowercase=True, strip_whitespace=True):
    """
    Parse comma-separated string into list.
    
    Args:
        text: Input string
        lowercase: If True, convert items to lowercase
        strip_whitespace: If True, strip whitespace from items
    
    Returns:
        List of parsed items (empty items removed)
    """
    if not text:
        return []
    
    try:
        items = [item for item in text.split(',') if item]
        
        if strip_whitespace:
            items = [item.strip() for item in items]
        
        if lowercase:
            items = [item.lower() for item in items]
        
        # Remove empty items
        items = [item for item in items if item]
        
        return items
    except Exception as e:
        print(f"Failed to parse comma-separated list: {e}")
        return []
    
def match_prefixes(string, prefixes):
    """
    Check if string starts with any of the given prefixes (case-insensitive).
    
    Args:
        string: String to check
        prefixes: List of prefixes to match

    Returns:
        True if matches, False otherwise
    """
    if not string or not prefixes:
        return False
    
    try:
        string_lower = string.lower()
        return any(string_lower.startswith(prefix.lower()) for prefix in prefixes)
    except Exception:
        return False

# =============================================================================
# Image Operations
# =============================================================================

def convert_srgb_to_linear(pixels):
    """
    Convert sRGB pixel data to linear color space and premultiply alpha.
    
    Args:
        pixels: Numpy array of shape (height, width, 4) with RGBA values
    
    Returns:
        Modified pixels array (same reference, modified in place)
    """
    # sRGB to linear conversion for RGB channels
    for i in range(3):
        mask = pixels[:, :, i] <= 0.04045
        pixels[:, :, i] = numpy.where(
            mask,
            pixels[:, :, i] / 12.92,
            numpy.power((pixels[:, :, i] + 0.055) / 1.055, 2.4)
        )
    
    # Premultiply RGB by alpha
    alpha = pixels[:, :, 3:4]
    pixels[:, :, :3] *= alpha
    
    return pixels


def convert_linear_to_srgb(pixels):
    """
    Convert linear pixel data to sRGB color space and unpremultiply alpha.
    
    Args:
        pixels: Numpy array of shape (height, width, 4) with RGBA values
    
    Returns:
        Modified pixels array (same reference, modified in place)
    """
    # Unpremultiply RGB by alpha
    alpha = pixels[:, :, 3:4]
    safe_alpha = numpy.where(alpha > 0.00001, alpha, 1.0)
    pixels[:, :, :3] /= safe_alpha
    
    # Linear to sRGB conversion for RGB channels
    for i in range(3):
        mask = pixels[:, :, i] <= 0.0031308
        pixels[:, :, i] = numpy.where(
            mask,
            pixels[:, :, i] * 12.92,
            1.055 * numpy.power(pixels[:, :, i], 1.0 / 2.4) - 0.055
        )
    
    return pixels


def alpha_composite_images(src_img, dst_img):
    """
    Composite src_img over dst_img using alpha blending (numpy optimized).
    Modifies dst_img in place.
    
    Handles bit depth conversion automatically:
    - If src is byte and dst is float: converts src from sRGB to linear and premultiplies alpha
    - If src is float and dst is byte: converts src from linear to sRGB and unpremultiplies alpha
    
    Formula: out = src + dst * (1 - src.a)
    
    Args:
        src_img: Source image to composite on top
        dst_img: Destination image (modified in place)
    
    Returns:
        True on success, False on failure
    """
    if not src_img or not dst_img:
        return False
    
    width = src_img.size[0]
    height = src_img.size[1]
    
    # Resize destination if needed
    if dst_img.size[0] != width or dst_img.size[1] != height:
        dst_img.scale(width, height)
    
    # Check if bit depth conversion is needed
    needs_conversion = src_img.is_float != dst_img.is_float
    
    # Use numpy for efficient pixel operations (Blender 2.83+)
    try:
        # Load pixels into numpy arrays
        src_pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
        dst_pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
        
        src_img.pixels.foreach_get(src_pixels)
        dst_img.pixels.foreach_get(dst_pixels)
        
        # Reshape to (height, width, 4) for easier manipulation
        src_pixels = src_pixels.reshape((height, width, 4))
        dst_pixels = dst_pixels.reshape((height, width, 4))
        
        # Handle bit depth conversion if needed
        if needs_conversion:
            if dst_img.is_float and not src_img.is_float:
                # Byte to float: sRGB to linear, then premultiply alpha
                convert_srgb_to_linear(src_pixels)
            else:
                # Float to byte: unpremultiply alpha, then linear to sRGB
                convert_linear_to_srgb(src_pixels)
        
        # Extract alpha channels
        src_alpha = src_pixels[:, :, 3:4]  # Keep dimension for broadcasting
        dst_alpha = dst_pixels[:, :, 3:4]
        
        # Compute output alpha: out_a = src_a + dst_a * (1 - src_a)
        out_alpha = src_alpha + dst_alpha * (1.0 - src_alpha)
        
        # Avoid division by zero
        # Where out_alpha is 0, the color doesn't matter (fully transparent)
        safe_out_alpha = numpy.where(out_alpha > 0.0, out_alpha, 1.0)
        
        # Compute output RGB: out_rgb = (src_rgb * src_a + dst_rgb * dst_a * (1 - src_a)) / out_a
        out_rgb = (src_pixels[:, :, :3] * src_alpha + 
                   dst_pixels[:, :, :3] * dst_alpha * (1.0 - src_alpha)) / safe_out_alpha
        
        # Where out_alpha is 0, set RGB to 0
        out_rgb = numpy.where(out_alpha > 0.0, out_rgb, 0.0)
        
        # Combine RGB and alpha
        dst_pixels[:, :, :3] = out_rgb
        dst_pixels[:, :, 3:4] = out_alpha
        
        # Write back to image
        dst_img.pixels.foreach_set(dst_pixels.ravel())
        return True
        
    except Exception as e:
        # Fallback to pixel-by-pixel if numpy fails
        print(f"      Numpy compositing failed, using fallback: {e}")
        try:
            src_pxs = list(src_img.pixels[:])
            dst_pxs = list(dst_img.pixels[:])
            
            if len(src_pxs) == len(dst_pxs):
                for pi in range(0, len(src_pxs), 4):
                    sr, sg, sb, sa = src_pxs[pi:pi+4]
                    dr, dg, db, da = dst_pxs[pi:pi+4]
                    
                    # Alpha composite: out = src + dst * (1 - src.a)
                    out_a = sa + da * (1.0 - sa)
                    if out_a > 0.0:
                        out_r = (sr * sa + dr * da * (1.0 - sa)) / out_a
                        out_g = (sg * sa + dg * da * (1.0 - sa)) / out_a
                        out_b = (sb * sa + db * da * (1.0 - sa)) / out_a
                    else:
                        out_r = out_g = out_b = 0.0
                    
                    dst_pxs[pi:pi+4] = [out_r, out_g, out_b, out_a]
                
                dst_img.pixels[:] = dst_pxs
                return True
            else:
                # Size mismatch, just overwrite
                dst_img.pixels[:] = src_pxs
                return True
        except Exception as e2:
            print(f"      Fallback compositing also failed: {e2}")
            return False


def copy_image_pixels(src_img, dst_img, resize_if_needed=True):
    """
    Copy pixels from src_img to dst_img.
    
    Handles bit depth conversion automatically:
    - If src is byte and dst is float: converts from sRGB to linear and premultiplies alpha
    - If src is float and dst is byte: converts from linear to sRGB and unpremultiplies alpha
    
    Args:
        src_img: Source image
        dst_img: Destination image (modified in place)
        resize_if_needed: If True, resize destination to match source
    
    Returns:
        True on success, False on failure
    """
    if not src_img or not dst_img:
        return False
    
    try:
        src_width = src_img.size[0]
        src_height = src_img.size[1]
        dst_width = dst_img.size[0]
        dst_height = dst_img.size[1]
        
        # Resize destination if needed
        if resize_if_needed and (dst_width != src_width or dst_height != src_height):
            dst_img.scale(src_width, src_height)
        
        # Check if bit depth conversion is needed
        needs_conversion = src_img.is_float != dst_img.is_float
        
        # Use numpy for fast copy if available
        try:
            pixel_count = src_width * src_height * 4
            src_pixels = numpy.empty(pixel_count, dtype=numpy.float32)
            src_img.pixels.foreach_get(src_pixels)
            
            # Handle bit depth conversion if needed
            if needs_conversion:
                # Reshape for easier manipulation
                src_pixels = src_pixels.reshape((src_height, src_width, 4))
                
                if dst_img.is_float and not src_img.is_float:
                    # Byte to float: sRGB to linear, then premultiply alpha
                    convert_srgb_to_linear(src_pixels)
                else:
                    # Float to byte: unpremultiply alpha, then linear to sRGB
                    convert_linear_to_srgb(src_pixels)
                
                # Flatten back
                src_pixels = src_pixels.ravel()
            
            dst_img.pixels.foreach_set(src_pixels)
            return True
        except Exception:
            # Fallback to direct pixel copy
            dst_img.pixels[:] = src_img.pixels[:]
            return True
            
    except Exception as e:
        print(f"Failed to copy image pixels: {e}")
        return False


def create_image(name, width, height, alpha=True, color=(0.0, 0.0, 0.0, 0.0), float_buffer=False):
    """
    Create a new image with specified properties.
    
    Args:
        name: Name for the image
        width: Image width in pixels
        height: Image height in pixels
        alpha: If True, create image with alpha channel
        color: Initial color (R, G, B, A)
        float_buffer: If True, use 32-bit float buffer
    
    Returns:
        Created image or None on failure
    """
    try:
        img = bpy.data.images.new(
            name=name,
            width=width,
            height=height,
            alpha=alpha,
            float_buffer=float_buffer
        )
        
        # Set initial color if not default
        if color != (0.0, 0.0, 0.0, 0.0):
            pixel_count = width * height
            if alpha:
                pixels = [color[0], color[1], color[2], color[3]] * pixel_count
            else:
                pixels = [color[0], color[1], color[2]] * pixel_count
            img.pixels[:] = pixels
        
        return img
    except Exception as e:
        print(f"Failed to create image '{name}': {e}")
        return None


def remove_image(image):
    """
    Safely remove an image datablock.
    
    Args:
        image: Image to remove
    
    Returns:
        True if removed, False otherwise
    """
    if not image:
        return False
    
    try:
        bpy.data.images.remove(image)
        return True
    except Exception as e:
        print(f"Failed to remove image: {e}")
        return False


def get_image_size(image):
    """
    Get image dimensions as tuple.
    
    Args:
        image: Image to query
    
    Returns:
        (width, height) tuple or (0, 0) if invalid
    """
    if not image:
        return (0, 0)
    
    try:
        return (image.size[0], image.size[1])
    except Exception:
        return (0, 0)


def resize_image(image, width, height):
    """
    Resize image to new dimensions.
    
    Args:
        image: Image to resize
        width: New width
        height: New height
    
    Returns:
        True on success, False on failure
    """
    if not image:
        return False
    
    try:
        image.scale(width, height)
        return True
    except Exception as e:
        print(f"Failed to resize image: {e}")
        return False


# =============================================================================
# Object & Scene Utilities
# =============================================================================

def store_selection_state(context):
    """
    Store current selection and active object state.
    
    Args:
        context: Blender context
    
    Returns:
        Dictionary with selection state or None on failure
    """
    try:
        return {
            'selected_objects': context.selected_objects.copy() if context.selected_objects else [],
            'active_object': context.view_layer.objects.active,
            'mode': context.mode
        }
    except Exception as e:
        print(f"Failed to store selection state: {e}")
        return None


def restore_selection_state(context, state):
    """
    Restore selection and active object state.
    
    Args:
        context: Blender context
        state: State dictionary from store_selection_state()
    
    Returns:
        True on success, False on failure
    """
    if not state:
        return False
    
    try:
        # Deselect all first
        try:
            bpy.ops.object.select_all(action='DESELECT')
        except Exception:
            pass
        
        # Restore selection
        for obj in state.get('selected_objects', []):
            try:
                obj.select_set(True)
            except Exception:
                pass
        
        # Restore active object
        try:
            context.view_layer.objects.active = state.get('active_object')
        except Exception:
            pass
        
        # Restore mode
        original_mode = state.get('mode')
        if original_mode and original_mode != 'OBJECT' and state.get('active_object'):
            try:
                bpy.ops.object.mode_set(mode=original_mode)
            except Exception:
                pass
        
        return True
    except Exception as e:
        print(f"Failed to restore selection state: {e}")
        return False


def ensure_object_mode(context):
    """
    Switch to object mode if not already.
    
    Args:
        context: Blender context
    
    Returns:
        True if in object mode, False on failure
    """
    try:
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        return True
    except Exception as e:
        print(f"Failed to switch to object mode: {e}")
        return False


# =============================================================================
# Material & Node Utilities
# =============================================================================

def find_image_texture_node(material, prefix=None):
    """
    Find image texture node in material, optionally by name prefix.
    
    Args:
        material: Material to search
        prefix: Optional name prefix to match
    
    Returns:
        First matching image texture node or None
    """
    if not material or not material.use_nodes:
        return None
    
    try:
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE':
                if prefix is None or node.name.startswith(prefix):
                    return node
        return None
    except Exception as e:
        print(f"Failed to find image texture node: {e}")
        return None


def get_connected_uv_map(tex_node):
    """
    Get UV map name connected to texture node.
    
    Args:
        tex_node: Image texture node
    
    Returns:
        UV map name or None if not found
    """
    if not tex_node or not hasattr(tex_node, 'inputs'):
        return None
    
    try:
        vector_input = tex_node.inputs.get('Vector')
        if vector_input and vector_input.is_linked:
            for link in vector_input.links:
                uv_node = link.from_node
                if uv_node.type == 'UVMAP':
                    return uv_node.uv_map
        return None
    except Exception as e:
        print(f"Failed to get connected UV map: {e}")
        return None


def find_all_image_texture_nodes(material):
    """
    Find all image texture nodes in a material.
    
    Args:
        material: Material to search
    
    Returns:
        List of image texture nodes (empty list if none found)
    """
    if not material or not material.use_nodes:
        return []
    
    try:
        texture_nodes = []
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE':
                texture_nodes.append(node)
        return texture_nodes
    except Exception as e:
        print(f"Failed to find image texture nodes: {e}")
        return []


def remove_nodes_by_name(material, node_names):
    """
    Remove specific nodes from material by name.
    
    Args:
        material: Material to modify
        node_names: List of node names to remove
    
    Returns:
        Number of nodes removed
    """
    if not material or not material.use_nodes:
        return 0
    
    removed_count = 0
    try:
        nodes = material.node_tree.nodes
        for node_name in node_names:
            node = nodes.get(node_name)
            if node:
                nodes.remove(node)
                removed_count += 1
        return removed_count
    except Exception as e:
        print(f"Failed to remove nodes: {e}")
        return removed_count

# =============================================================================
# UV Operations
# =============================================================================

def ensure_uv_layer(obj, uv_name, make_active=False):
    """
    Create UV layer if it doesn't exist.
    
    Args:
        obj: Object to modify
        uv_name: Name of UV layer
        make_active: If True, make the UV layer active
    
    Returns:
        UV layer or None on failure
    """
    if not obj or obj.type != 'MESH':
        return None
    
    try:
        uv_layer = obj.data.uv_layers.get(uv_name)
        if not uv_layer:
            uv_layer = obj.data.uv_layers.new(name=uv_name)
        
        if make_active and uv_layer:
            obj.data.uv_layers.active = uv_layer
        
        return uv_layer
    except Exception as e:
        print(f"Failed to ensure UV layer '{uv_name}': {e}")
        return None


def remove_uv_layer(obj, uv_name):
    """
    Safely remove UV layer.
    
    Args:
        obj: Object to modify
        uv_name: Name of UV layer to remove
    
    Returns:
        True if removed, False otherwise
    """
    if not obj or obj.type != 'MESH':
        return False
    
    try:
        uv_layer = obj.data.uv_layers.get(uv_name)
        if uv_layer:
            obj.data.uv_layers.remove(uv_layer)
            return True
        return False
    except Exception as e:
        print(f"Failed to remove UV layer '{uv_name}': {e}")
        return False


def get_active_uv_layer_name(obj):
    """
    Get active UV layer name.
    
    Args:
        obj: Object to query
    
    Returns:
        UV layer name or empty string
    """
    if not obj or obj.type != 'MESH':
        return ""
    
    try:
        active_uv = obj.data.uv_layers.active
        return active_uv.name if active_uv else ""
    except Exception:
        return ""


# =============================================================================
# Vertex Color Operations
# =============================================================================

def ensure_vertex_color_layer(obj, vcol_name):
    """
    Create vertex color layer if it doesn't exist.
    
    Args:
        obj: Object to modify
        vcol_name: Name of vertex color layer
    
    Returns:
        Vertex color layer or None on failure
    """
    if not obj or obj.type != 'MESH':
        return None
    
    try:
        vcol = obj.data.vertex_colors.get(vcol_name)
        if not vcol:
            vcol = obj.data.vertex_colors.new(name=vcol_name)
        return vcol
    except Exception as e:
        print(f"Failed to ensure vertex color layer '{vcol_name}': {e}")
        return None


def remove_vertex_color_layer(obj, vcol_name):
    """
    Safely remove vertex color layer.
    
    Args:
        obj: Object to modify
        vcol_name: Name of vertex color layer to remove
    
    Returns:
        True if removed, False otherwise
    """
    if not obj or obj.type != 'MESH':
        return False
    
    try:
        vcol = obj.data.vertex_colors.get(vcol_name)
        if vcol:
            obj.data.vertex_colors.remove(vcol)
            return True
        return False
    except Exception as e:
        print(f"Failed to remove vertex color layer '{vcol_name}': {e}")
        return False


# =============================================================================
# Object Management
# =============================================================================

def remove_object(obj):
    """
    Safely remove an object from the scene.
    
    Args:
        obj: Object to remove
    
    Returns:
        True if removed, False otherwise
    """
    if not obj:
        return False
    
    try:
        # Check if object still exists in bpy.data.objects
        if obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj)
            return True
        return False
    except Exception as e:
        print(f"Failed to remove object: {e}")
        return False


def remove_material(material):
    """
    Safely remove a material datablock.
    
    Args:
        material: Material to remove
    
    Returns:
        True if removed, False otherwise
    """
    if not material:
        return False
    
    try:
        bpy.data.materials.remove(material)
        return True
    except Exception as e:
        print(f"Failed to remove material: {e}")
        return False


def get_or_create_collection(collection_name, link_to_scene=True):
    """
    Get existing collection or create a new one.
    
    Args:
        collection_name: Name of the collection
        link_to_scene: If True, link to scene collection if creating new
    
    Returns:
        The collection object
    """
    try:
        collection = bpy.data.collections.get(collection_name)
        if not collection:
            collection = bpy.data.collections.new(collection_name)
            if link_to_scene:
                bpy.context.scene.collection.children.link(collection)
        return collection
    except Exception as e:
        print(f"Failed to get/create collection '{collection_name}': {e}")
        return None

def remove_collection(collection):
    """
    Safely remove a collection from bpy.data.collections.
    
    Args:
        collection: Collection to remove
    Returns:
        True if removed, False otherwise
    """
    if not collection:
        return False
    
    try:
        bpy.data.collections.remove(collection)
        return True
    except Exception as e:
        print(f"Failed to remove collection: {e}")
        return False

# =============================================================================
# Validation & Polling
# =============================================================================

def is_valid_mesh_object(obj):
    """
    Check if object is a valid mesh with faces.
    
    Args:
        obj: Object to validate
    
    Returns:
        True if valid mesh with faces, False otherwise
    """
    if not obj or obj.type != 'MESH':
        return False
    
    try:
        # Check if mesh has polygons (faces)
        if not hasattr(obj.data, 'polygons') or len(obj.data.polygons) == 0:
            return False
        return True
    except Exception:
        return False


def has_uv_layers(obj):
    """
    Check if object has UV layers.
    
    Args:
        obj: Object to check
    
    Returns:
        True if has UV layers, False otherwise
    """
    if not obj or obj.type != 'MESH':
        return False
    
    try:
        return bool(obj.data.uv_layers)
    except Exception:
        return False


def has_material_slots(obj):
    """
    Check if object has material slots.
    
    Args:
        obj: Object to check
    
    Returns:
        True if has material slots, False otherwise
    """
    if not obj or obj.type != 'MESH':
        return False
    
    try:
        return bool(obj.data.materials)
    except Exception:
        return False


def validate_camera(camera):
    """
    Validate camera object.
    
    Args:
        camera: Object to validate as camera
    
    Returns:
        True if valid camera, False otherwise
    """
    if not camera:
        return False
    
    try:
        return camera.type == 'CAMERA'
    except Exception:
        return False


# =============================================================================
# Viewport & Rendering
# =============================================================================

def get_viewport_shading_type(context):
    """
    Get current viewport shading type.
    
    Args:
        context: Blender context
    
    Returns:
        Shading type string or None
    """
    try:
        if context.area and context.area.type == 'VIEW_3D':
            space = context.space_data
            if space and space.type == 'VIEW_3D':
                return space.shading.type
        return None
    except Exception as e:
        print(f"Failed to get viewport shading type: {e}")
        return None


def refresh_viewport(context):
    """
    Force viewport refresh.
    
    Args:
        context: Blender context
    
    Returns:
        True on success, False on failure
    """
    try:
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return True
    except Exception as e:
        print(f"Failed to refresh viewport: {e}")
        return False


# =============================================================================
# Render Settings Management
# =============================================================================

class RenderSettings:
    """Store and restore render settings"""
    def __init__(self):
        self.engine = None
        self.resolution_x = None
        self.resolution_y = None
        self.view_transform = None
        self.film_transparent = None
        self.eevee_taa_samples = None
        self.camera = None
    
    def store(self, scene):
        """Store current render settings"""
        self.engine = scene.render.engine
        self.resolution_x = scene.render.resolution_x
        self.resolution_y = scene.render.resolution_y
        self.view_transform = scene.view_settings.view_transform
        self.film_transparent = scene.render.film_transparent
        self.eevee_taa_samples = scene.eevee.taa_render_samples
        self.camera = scene.camera
        print("  Stored original render settings")
    
    def restore(self, scene):
        """Restore original render settings"""
        scene.render.engine = self.engine
        scene.render.resolution_x = self.resolution_x
        scene.render.resolution_y = self.resolution_y
        scene.view_settings.view_transform = self.view_transform
        scene.render.film_transparent = self.film_transparent
        scene.eevee.taa_render_samples = self.eevee_taa_samples
        scene.camera = self.camera
        print("  Restored original render settings")


def is_in_camera_view(context):
    """
    Check if viewport is in camera view.
    
    Args:
        context: Blender context
    
    Returns:
        True if in camera view, False otherwise
    """
    try:
        if context.area and context.area.type == 'VIEW_3D':
            space = context.space_data
            if space and space.type == 'VIEW_3D':
                return space.region_3d.view_perspective == 'CAMERA'
        return False
    except Exception:
        return False


def switch_to_camera_view(context):
    """
    Switch viewport to camera view.
    
    Args:
        context: Blender context
    
    Returns:
        True on success, False on failure
    """
    try:
        if context.area and context.area.type == 'VIEW_3D':
            bpy.ops.view3d.view_camera()
            return True
        return False
    except Exception as e:
        print(f"Failed to switch to camera view: {e}")
        return False


# =============================================================================
# Camera & Visibility Utilities
# =============================================================================

def get_enabled_objects(context, property_name='cam_proj_paint', enabled_attr='enabled'):
    """
    Return a list of objects that have a specific property enabled.
    
    Args:
        context: Blender context
        property_name: Name of the property group on the object (default: 'cam_proj_paint')
        enabled_attr: Name of the boolean attribute within the property (default: 'enabled')
    
    Returns:
        List of enabled objects
    """
    enabled_objects = []
    try:
        for obj in context.scene.objects:
            prop_group = getattr(obj, property_name, None)
            if prop_group and getattr(prop_group, enabled_attr, False):
                enabled_objects.append(obj)
    except Exception as e:
        print(f"Failed to get enabled objects: {e}")
    
    return enabled_objects


def get_visible_objects_from_camera(context, camera=None):
    """
    Return a list of mesh objects whose bounding-box corners or vertices are
    inside the camera's view frustum.
    
    Uses a robust visibility test: first tests all bounding-box corners, then
    samples mesh vertices if needed. This detects objects that are partially
    in-frame while remaining reasonably fast.
    
    Args:
        context: Blender context
        camera: Camera object to use (if None, uses scene.camera)
    
    Returns:
        List of visible mesh objects
    """
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    
    cam = camera or context.scene.camera
    if not cam:
        return []

    visible = []
    for obj in context.scene.objects:
        # Skip meshes with no faces (e.g. loose verts/edges). Many operators
        # (UV projection, data_transfer, etc.) require at least one face and
        # will fail their poll() checks when run against such objects.
        if not is_valid_mesh_object(obj):
            continue
        if obj.hide_get() or obj.hide_viewport:
            continue

        # Robust visibility test: first test all bounding-box corners, then
        # sample mesh vertices if needed. Using the bbox center misses objects
        # that are partially in-frame (e.g. half-in-frame), so checking corners
        # and sampled vertices gives much better coverage while remaining
        # reasonably fast.
        try:
            bbox_world = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        except Exception:
            bbox_world = [obj.matrix_world.translation]

        in_view = False

        # 1) Check bounding box corners
        for p in bbox_world:
            try:
                co_ndc = world_to_camera_view(context.scene, cam, p)
                if 0.0 <= co_ndc.x <= 1.0 and 0.0 <= co_ndc.y <= 1.0 and co_ndc.z > 0.0:
                    in_view = True
                    break
            except Exception:
                # ignore individual projection failures
                continue

        # 2) If no bbox corner is inside the view, sample some vertices
        # (use up to a reasonable limit) to detect partially visible meshes
        if not in_view:
            try:
                verts = getattr(obj.data, 'vertices', None)
                if verts and len(verts) > 0:
                    max_samples = 64
                    total = len(verts)
                    step = max(1, total // max_samples)
                    for i in range(0, total, step):
                        v = verts[i]
                        p = obj.matrix_world @ v.co
                        try:
                            co_ndc = world_to_camera_view(context.scene, cam, p)
                            if 0.0 <= co_ndc.x <= 1.0 and 0.0 <= co_ndc.y <= 1.0 and co_ndc.z > 0.0:
                                in_view = True
                                break
                        except Exception:
                            continue
            except Exception:
                # sampling failed; fall back to skipping the object
                in_view = False

        if in_view:
            visible.append(obj)

    return visible


def calculate_camera_visibility(obj, camera, fill=None, vcol_name=None):
    """
    Calculate a per-vertex visibility mask (stored in a vertex color layer)
    based on whether faces are camera-facing.
    
    If use_camera_facing is False, all faces are considered visible.
    
    Args:
        obj: Object to calculate visibility for
        camera: Camera object
        fill: If not None, fill value (0.0-1.0) for all vertices instead of calculating visibility
        vcol_name: Name of vertex color layer to store results (default: PROJECTION_VIS_VCOL_NAME)
    
    Returns:
        True on success, False on failure
    """
    import bmesh
    
    if vcol_name is None:
        vcol_name = PROJECTION_VIS_VCOL_NAME
    
    try:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        
        camera_pos = None
        if camera:
            camera_pos = camera.matrix_world.translation

        # Per-face visibility determined only by face normal (no occlusion/raycasts)
        face_visibility = {}

        for face in bm.faces:
            try:
                # Sample points: face center + each vertex position (world space)
                samples = []
                face_center_local = face.calc_center_median()
                face_center_world = obj.matrix_world @ face_center_local
                samples.append(face_center_world)
                for v in face.verts:
                    samples.append(obj.matrix_world @ v.co)

                face_normal_world = (obj.matrix_world.to_3x3() @ face.normal).normalized()

                sample_vis_max = 0.0
                sample_count = 0

                for sample_pt in samples:
                    if fill is None:
                        to_camera = camera_pos - sample_pt
                        if to_camera.length == 0.0:
                            facing = True
                        else:
                            facing = face_normal_world.dot(to_camera.normalized()) > 0.0
                        sample_vis = 1.0 if facing else 0.0
                    else:
                        sample_vis = min(max(fill, 0.0), 1.0)

                    if sample_vis > sample_vis_max:
                        sample_vis_max = sample_vis
                    sample_count += 1

                face_visibility[face.index] = sample_vis_max if sample_count > 0 else 0.0
            except Exception:
                face_visibility[face.index] = 0.0

        # Per-vertex visibility: use the maximum visibility of connected faces
        vertex_visibility = {}
        for vert in bm.verts:
            connected = vert.link_faces
            if connected:
                maxv = max(face_visibility.get(f.index, 0.0) for f in connected)
                vertex_visibility[vert.index] = maxv
            else:
                vertex_visibility[vert.index] = 0.0

        # Ensure vertex color layer exists
        vcol = ensure_vertex_color_layer(obj, vcol_name)
        if not vcol:
            bm.free()
            return False

        # Write per-loop colors
        for poly in obj.data.polygons:
            for loop_idx in poly.loop_indices:
                loop = obj.data.loops[loop_idx]
                vert_idx = loop.vertex_index
                vis = vertex_visibility.get(vert_idx, 0.0)
                # RGBA: store visibility in RGB channels; alpha kept at 1.0
                vcol.data[loop_idx].color = (vis, vis, vis, 1.0)

        bm.free()
        return True
    except Exception as e:
        print(f"Failed to calculate camera visibility: {e}")
        try:
            bm.free()
        except Exception:
            pass
        return False
