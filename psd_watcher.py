"""
PSD File Watcher Module

Uses watchdog library to monitor PSD files for changes.
"""

import bpy
import os
import time

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    print("WARNING: watchdog library not available. Auto-reload will not work.")
    print("Install with: <blender_path>/python/bin/python.exe -m pip install watchdog")


class PSDFileHandler(FileSystemEventHandler):
    """Handler for PSD file modification events"""
    
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.last_modified = {}
        self.debounce_time = 1.0  # Wait 1s before triggering to avoid multiple events (PSD files are larger)
        
    def on_modified(self, event):
        if event.is_directory:
            return
        
        filepath = event.src_path
        current_time = time.time()
        
        # Debounce - ignore if we just processed this file
        if filepath in self.last_modified:
            if current_time - self.last_modified[filepath] < self.debounce_time:
                return
        
        self.last_modified[filepath] = current_time
        
        # Check if this is a PSD file
        ext = os.path.splitext(filepath)[1].lower()
        if ext in {'.psd', '.psb'}:
            print(f"PSD file modified: {filepath}")
            # Call the callback in the main thread
            self.callback(filepath)


class PSDWatcher:
    """Watches PSD files for modifications and triggers callbacks"""
    
    def __init__(self):
        self.observer = None
        self.watched_paths = {}  # filepath -> watch handle
        self.handler = None
        
    def start_watching(self, filepath, callback):
        """Start watching a specific PSD file for changes"""
        if not WATCHDOG_AVAILABLE:
            print("Watchdog not available - cannot start file watching")
            return False
        
        # Use filepath directly (should already be absolute)
        abs_path = os.path.abspath(filepath)
        if not os.path.exists(abs_path):
            print(f"PSD file does not exist: {abs_path}")
            return False
        
        # Get the directory to watch
        watch_dir = os.path.dirname(abs_path)
        
        # Create observer if needed
        if self.observer is None:
            self.observer = Observer()
            self.handler = PSDFileHandler(callback)
            self.observer.start()
        
        # Add watch if not already watching this directory
        if watch_dir not in self.watched_paths:
            watch = self.observer.schedule(self.handler, watch_dir, recursive=False)
            self.watched_paths[watch_dir] = watch
            print(f"Started watching PSD directory: {watch_dir}")
        
        return True
    
    def stop_watching(self):
        """Stop watching all files"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            self.watched_paths.clear()
            print("Stopped PSD file watching")
    
    def is_watching(self):
        """Check if currently watching any files"""
        return self.observer is not None and self.observer.is_alive()


# Global watcher instance
_watcher = None


def get_watcher():
    """Get or create the global watcher instance"""
    global _watcher
    if _watcher is None:
        _watcher = PSDWatcher()
    return _watcher


def start_watching_psd_file(scene):
    """Start watching the PSD file for changes"""
    psd_file_path = scene.cam_proj_paint.projection_psd_file
    
    if not psd_file_path:
        print("No PSD file path specified - cannot watch")
        return False
    
    if not os.path.exists(psd_file_path):
        print(f"PSD file does not exist: {psd_file_path}")
        return False
    
    def on_psd_changed(filepath):
        """Callback when PSD file is modified"""
        # Schedule reload in main thread
        def reload_and_apply():
            try:
                # Verify this is still the current PSD file
                current_psd_path = bpy.context.scene.cam_proj_paint.projection_psd_file
                if not current_psd_path:
                    return None
                
                if os.path.abspath(current_psd_path) != filepath:
                    return None
                
                print(f"\n{'='*50}")
                print(f"PSD file changed, auto-reloading layers...")
                print(f"{'='*50}")
                
                # Use the reload operator which extracts and applies all PSD layers
                bpy.ops.camprojpaint.reload_projection_image()
                
            except Exception as e:
                print(f"Error reloading PSD file: {e}")
                import traceback
                traceback.print_exc()
            
            return None  # Don't repeat
        
        # Use timer to run in main thread (wait a bit longer for PSD files to finish saving)
        bpy.app.timers.register(reload_and_apply, first_interval=0.5)
    
    watcher = get_watcher()
    return watcher.start_watching(psd_file_path, on_psd_changed)


def stop_watching():
    """Stop watching all files"""
    watcher = get_watcher()
    watcher.stop_watching()


def is_watching():
    """Check if file watching is active"""
    if not WATCHDOG_AVAILABLE:
        return False
    watcher = get_watcher()
    return watcher.is_watching()
