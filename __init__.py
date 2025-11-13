"""
Camera Projection Paint Addon

A complete addon for camera-based projection painting workflow with EEVEE baking support.
"""

bl_info = {
    "name": "Camera Projection Paint",
    "author": "smewed",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Projection Paint",
    "description": "Project painted images back onto objects as textures using camera-based UV projection with EEVEE/Cycles baking",
    "category": "Paint",
}

# Import modules
if "bpy" in locals():
    import importlib
    if "camera_projection_paint" in locals():
        importlib.reload(camera_projection_paint)
    if "uv_bake_eevee" in locals():
        importlib.reload(uv_bake_eevee)
else:
    from . import camera_projection_paint
    from . import uv_bake_eevee

import bpy


def register():
    """Register all addon modules"""
    camera_projection_paint.register()
    uv_bake_eevee.register()


def unregister():
    """Unregister all addon modules"""
    uv_bake_eevee.unregister()
    camera_projection_paint.unregister()


if __name__ == "__main__":
    register()
