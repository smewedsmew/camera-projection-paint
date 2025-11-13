"""
PSD file handling for multi-layer projection.

This module provides functionality to extract individual layers from PSD files
and convert them to Blender Image datablocks for use in projection workflows.
"""

import bpy
import os
import numpy as np
from PIL import Image

# Try to import psd-tools
try:
    from psd_tools import PSDImage
    PSD_AVAILABLE = True
except ImportError:
    PSD_AVAILABLE = False
    print("Warning: psd-tools not installed. PSD layer support disabled.")


def is_psd_available():
    """
    Check if psd-tools is available.
    
    Returns:
        bool: True if psd-tools is available, False otherwise
    """
    return PSD_AVAILABLE


def get_psd_layer_list(psd_filepath):
    """
    Get list of layer names from PSD file without extracting pixel data.
    
    Args:
        psd_filepath: Path to PSD file
    
    Returns:
        list: List of tuples (layer_name, is_group)
              Returns empty list on failure
    """
    if not PSD_AVAILABLE:
        print("psd-tools not available")
        return []
    
    if not os.path.exists(psd_filepath):
        print(f"PSD file not found: {psd_filepath}")
        return []
    
    try:
        psd = PSDImage.open(psd_filepath)
        layers = []
        
        def traverse_layers(layer_list, prefix=""):
            """Recursively traverse layer tree"""
            for layer in layer_list:
                layer_name = prefix + layer.name
                
                # Check if it's a group
                is_group = hasattr(layer, 'is_group') and layer.is_group()
                
                layers.append((layer_name, is_group))
                
                # Recursively traverse group layers
                if is_group and hasattr(layer, '__iter__'):
                    traverse_layers(layer, prefix=layer_name + "/")
        
        traverse_layers(psd)
        
        # print(f"Found {len(layers)} layers in PSD file: {psd_filepath}")
        return layers
        
    except Exception as e:
        print(f"Failed to read PSD layer list: {e}")
        return []


def extract_single_layer(psd_filepath, layer_name, as_blender_image=True, image_name=None):
    """
    Extract a single layer from PSD file.
    
    Args:
        psd_filepath: Path to PSD file
        layer_name: Name of the layer to extract
        as_blender_image: If True, return as Blender Image datablock
        image_name: Optional name for the Blender image (default: layer_name)
    
    Returns:
        bpy.types.Image if as_blender_image=True, PIL.Image otherwise
        Returns None on failure
    """
    if not PSD_AVAILABLE:
        print("psd-tools not available")
        return None
    
    if not os.path.exists(psd_filepath):
        print(f"PSD file not found: {psd_filepath}")
        return None
    
    try:
        psd = PSDImage.open(psd_filepath)
        
        # Find the layer by name (handles both simple names and paths like "group/layer")
        target_layer = None
        
        def find_layer_by_path(layer_list, target_path, current_prefix=""):
            """Recursively search for layer by full path"""
            for layer in layer_list:
                full_path = current_prefix + layer.name
                
                # Check if this is the target layer
                if full_path == target_path:
                    return layer
                
                # Check in groups
                if hasattr(layer, 'is_group') and layer.is_group():
                    if hasattr(layer, '__iter__'):
                        found = find_layer_by_path(layer, target_path, current_prefix=full_path + "/")
                        if found:
                            return found
            return None
        
        target_layer = find_layer_by_path(psd, layer_name)
        
        if not target_layer:
            print(f"Layer '{layer_name}' not found in PSD file")
            return None
        
        # Check if layer is a group
        if hasattr(target_layer, 'is_group') and target_layer.is_group():
            print(f"Layer '{layer_name}' is a group, cannot extract as image")
            return None
        
        # Get layer opacity (0-255)
        layer_opacity = getattr(target_layer, 'opacity', 255) / 255.0
        
        # Convert layer to PIL Image
        layer_image = target_layer.topil()
        
        # Get layer's bounding box (offset from canvas origin)
        bbox = target_layer.bbox
        if bbox:
            left, top, right, bottom = bbox
        else:
            # If no bbox, layer is empty or full canvas
            left, top = 0, 0
        
        # Create full canvas-sized transparent image
        canvas_width = psd.width
        canvas_height = psd.height
        full_image = Image.new('RGBA', (canvas_width, canvas_height), (0, 0, 0, 0))
        
        # Paste the layer image at the correct position
        if layer_image:
            # Convert layer to RGBA if needed
            if layer_image.mode != 'RGBA':
                layer_image = layer_image.convert('RGBA')
            
            # Apply layer opacity to alpha channel
            if layer_opacity < 1.0:
                # Convert to numpy array for efficient processing
                layer_array = np.array(layer_image, dtype=np.float32)
                # Multiply alpha channel by layer opacity
                layer_array[:, :, 3] *= layer_opacity
                # Convert back to PIL Image
                layer_image = Image.fromarray(layer_array.astype(np.uint8), 'RGBA')
            
            full_image.paste(layer_image, (left, top))
            
        if not as_blender_image:
            return full_image
        
        # Convert PIL Image to Blender Image
        return pil_image_to_blender(full_image, image_name or layer_name)
        
    except Exception as e:
        print(f"Failed to extract layer '{layer_name}': {e}")
        return None


def extract_psd_layers(psd_filepath, as_blender_images=True):
    """
    Extract all layers from PSD file.
    
    Args:
        psd_filepath: Path to PSD file
        as_blender_images: If True, return as Blender Image datablocks
    
    Returns:
        dict: {layer_name: bpy.types.Image or PIL.Image}
              Returns empty dict on failure
    """
    if not PSD_AVAILABLE:
        print("psd-tools not available")
        return {}
    
    if not os.path.exists(psd_filepath):
        print(f"PSD file not found: {psd_filepath}")
        return {}
    
    try:
        psd = PSDImage.open(psd_filepath)
        layers = {}
        
        def extract_from_list(layer_list, prefix=""):
            """Recursively extract layers"""
            for layer in layer_list:
                layer_name = prefix + layer.name
                
                # Skip groups
                if hasattr(layer, 'is_group') and layer.is_group():
                    if hasattr(layer, '__iter__'):
                        extract_from_list(layer, prefix=layer_name + "/")
                    continue
                
                try:
                    pil_image = layer.topil()
                    
                    if as_blender_images:
                        blender_image = pil_image_to_blender(pil_image, layer_name)
                        if blender_image:
                            layers[layer_name] = blender_image
                    else:
                        layers[layer_name] = pil_image
                        
                except Exception as e:
                    print(f"Failed to extract layer '{layer_name}': {e}")
                    continue
        
        extract_from_list(psd)
        
        print(f"Extracted {len(layers)} layers from PSD file")
        return layers
        
    except Exception as e:
        print(f"Failed to extract PSD layers: {e}")
        return {}


def pil_image_to_blender(pil_image, name):
    """
    Convert PIL Image to Blender Image datablock.
    
    Args:
        pil_image: PIL Image object
        name: Name for the Blender image
    
    Returns:
        bpy.types.Image or None on failure
    """
    try:
        # Convert to RGBA if needed
        if pil_image.mode != 'RGBA':
            pil_image = pil_image.convert('RGBA')
        
        width, height = pil_image.size
        
        # Create or get existing Blender image
        blender_image = bpy.data.images.get(name)
        if blender_image:
            # Resize if needed
            if blender_image.size[0] != width or blender_image.size[1] != height:
                blender_image.scale(width, height)
        else:
            # Create new image
            blender_image = bpy.data.images.new(
                name=name,
                width=width,
                height=height,
                alpha=True,
                float_buffer=False
            )
        
        # Convert PIL image to numpy array
        # PSD layers from psd-tools are already in straight/unassociated alpha format
        # (RGB values are NOT premultiplied), so we can use them directly
        pixels = np.array(pil_image, dtype=np.float32) / 255.0
        
        # Flip vertically (Blender images are stored bottom-to-top)
        pixels = np.flipud(pixels)
        
        # Flatten to 1D array
        pixels = pixels.ravel()
        
        # Set pixels
        blender_image.pixels.foreach_set(pixels)
        blender_image.update()
        
        # Pack image to prevent issues with missing file paths
        blender_image.pack()
        
        return blender_image
        
    except Exception as e:
        print(f"Failed to convert PIL image to Blender image: {e}")
        return None


def reload_psd_layers(psd_filepath, layer_mapping):
    """
    Reload specific layers from PSD file based on mapping.
    
    Args:
        psd_filepath: Path to PSD file
        layer_mapping: Dict mapping {layer_name: blender_image}
                      Blender images will be updated in place
    
    Returns:
        int: Number of layers successfully reloaded
    """
    if not PSD_AVAILABLE:
        print("psd-tools not available")
        return 0
    
    if not os.path.exists(psd_filepath):
        print(f"PSD file not found: {psd_filepath}")
        return 0
    
    reloaded_count = 0
    
    for layer_name, blender_image in layer_mapping.items():
        if not blender_image:
            continue
        
        try:
            # Extract layer as PIL image
            pil_image = extract_single_layer(
                psd_filepath, 
                layer_name, 
                as_blender_image=False
            )
            
            if not pil_image:
                continue
            
            # Convert to RGBA
            if pil_image.mode != 'RGBA':
                pil_image = pil_image.convert('RGBA')
            
            width, height = pil_image.size
            
            # Resize Blender image if needed
            if blender_image.size[0] != width or blender_image.size[1] != height:
                blender_image.scale(width, height)
            
            # Convert PIL image to numpy array
            # PSD layers from psd-tools are already in straight/unassociated alpha format
            pixels = np.array(pil_image, dtype=np.float32) / 255.0
            
            # Flip vertically
            pixels = np.flipud(pixels)
            
            # Flatten and set pixels
            pixels = pixels.ravel()
            blender_image.pixels.foreach_set(pixels)
            blender_image.update()
            
            reloaded_count += 1
            
        except Exception as e:
            print(f"Failed to reload layer '{layer_name}': {e}")
            continue
    
    print(f"Reloaded {reloaded_count}/{len(layer_mapping)} PSD layers")
    return reloaded_count


def get_psd_info(psd_filepath):
    """
    Get basic information about a PSD file.
    
    Args:
        psd_filepath: Path to PSD file
    
    Returns:
        dict: {
            'width': int,
            'height': int,
            'channels': int,
            'depth': int,
            'color_mode': str,
            'layer_count': int
        }
        Returns None on failure
    """
    if not PSD_AVAILABLE:
        return None
    
    if not os.path.exists(psd_filepath):
        return None
    
    try:
        psd = PSDImage.open(psd_filepath)
        
        # Count layers (excluding groups)
        layer_count = 0
        def count_layers(layer_list):
            nonlocal layer_count
            for layer in layer_list:
                if hasattr(layer, 'is_group') and layer.is_group():
                    if hasattr(layer, '__iter__'):
                        count_layers(layer)
                else:
                    layer_count += 1
        
        count_layers(psd)
        
        return {
            'width': psd.width,
            'height': psd.height,
            'channels': psd.channels,
            'depth': psd.depth,
            'color_mode': str(psd.color_mode),
            'layer_count': layer_count
        }
        
    except Exception as e:
        print(f"Failed to get PSD info: {e}")
        return None
