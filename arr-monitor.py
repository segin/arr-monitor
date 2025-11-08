#!/usr/bin/env python3

"""
*arr Media Manager File Transfer Monitor
Monitors file write operations for Sonarr, Radarr, Lidarr, etc.
"""

import os
import sys
import time
import curses
import psutil
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Media manager process names to auto-detect
ARR_MANAGERS = [
    'Sonarr', 'Radarr', 'Lidarr', 'Readarr', 
    'Prowlarr', 'Bazarr', 'Whisparr'
]

# File extensions to ignore (databases, logs, etc.)
IGNORE_EXTENSIONS = {
    '.db', '.db-wal', '.db-shm', '.db-journal',
    '.log', '.txt', '.xml', '.json', '.conf'
}

class FileTransferInfo:
    """Tracks information about a file being written"""
    def __init__(self, fd, filepath, position, size, target_size=None):
        self.fd = fd
        self.filepath = filepath
        self.position = position
        self.size = size
        self.target_size = target_size if target_size is not None else size  # Lock in target
        self.last_position = position
        self.last_time = time.time()
        self.speed = 0
        self.first_seen = time.time()
        
    def update(self, position, size):
        """Update position, size, and calculate speed"""
        current_time = time.time()
        time_delta = current_time - self.last_time
        
        # Use actual file size as position (more reliable for sequential writes)
        actual_position = size
        
        if time_delta > 0:
            bytes_written = actual_position - self.last_position
            if bytes_written > 0:
                self.speed = bytes_written / time_delta
        
        self.last_position = actual_position
        self.position = actual_position
        self.size = size
        self.last_time = current_time
        # NOTE: target_size stays constant - it was set at initialization
    
    @property
    def percent(self):
        """Calculate percentage complete"""
        if self.target_size > 0:
            return (self.position / self.target_size) * 100
        return 0
    
    @property
    def eta_seconds(self):
        """Calculate ETA in seconds"""
        if self.speed > 0 and self.target_size > self.position:
            remaining = self.target_size - self.position
            return remaining / self.speed
        return None
    
    @property
    def filename(self):
        """Get just the filename"""
        return os.path.basename(self.filepath)

def format_size(bytes_val):
    """Format bytes as human-readable size"""
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PiB"

def format_speed(bytes_per_sec):
    """Format bytes/sec as human-readable speed"""
    return format_size(bytes_per_sec) + "/s"

def format_time(seconds):
    """Format seconds as HH:MM:SS or MM:SS"""
    if seconds is None:
        return "--:--"
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def should_ignore_file(filepath):
    """Check if file should be ignored"""
    path = Path(filepath)
    return path.suffix.lower() in IGNORE_EXTENSIONS

def find_arr_processes():
    """Find all running *arr manager processes"""
    found = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name']
            if name in ARR_MANAGERS:
                found.append((proc.info['pid'], name))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found

def get_open_files(pid):
    """Get files currently being written by the process"""
    open_files = {}
    read_files = {}  # Track files being read (potential sources)
    
    try:
        # Read /proc/<pid>/fd and /proc/<pid>/fdinfo
        fd_dir = Path(f"/proc/{pid}/fd")
        fdinfo_dir = Path(f"/proc/{pid}/fdinfo")
        
        if not fd_dir.exists() or not fdinfo_dir.exists():
            return {}
        
        # First pass: collect all file info
        all_fds = {}
        for fd_link in fd_dir.iterdir():
            try:
                fd = fd_link.name
                filepath = fd_link.resolve()
                
                # Skip if not a regular file
                if not filepath.is_file():
                    continue
                
                # Skip ignored files
                if should_ignore_file(str(filepath)):
                    continue
                
                # Get file size early
                try:
                    file_size = filepath.stat().st_size
                except (OSError, FileNotFoundError):
                    continue
                
                # Read fdinfo to get position and flags
                fdinfo_path = fdinfo_dir / fd
                if not fdinfo_path.exists():
                    continue
                
                position = 0
                flags = 0
                
                try:
                    with open(fdinfo_path, 'r') as f:
                        for line in f:
                            if line.startswith('pos:'):
                                position = int(line.split()[1])
                            elif line.startswith('flags:'):
                                flags = int(line.split()[1], 8)  # Octal
                except (OSError, ValueError):
                    continue
                
                # Check access mode
                access_mode = flags & 0o3
                
                all_fds[fd] = {
                    'filepath': filepath,
                    'file_size': file_size,
                    'position': position,
                    'access_mode': access_mode,
                    'flags': flags
                }
            
            except (PermissionError, OSError):
                continue
        
        # Second pass: identify read and write files
        for fd, info in all_fds.items():
            access_mode = info['access_mode']
            
            # Read-only files (potential sources)
            if access_mode == 0:
                # Store by filename for matching
                filename = info['filepath'].name
                read_files[filename] = info['file_size']
            
            # Writable files (destinations)
            elif access_mode in (1, 2):  # O_WRONLY or O_RDWR
                filepath = info['filepath']
                file_size = info['file_size']
                position = info['position']
                
                # Use current file size as position (more reliable)
                current_pos = file_size
                
                # Try to find matching source file to get target size
                filename = filepath.name
                target_size = read_files.get(filename, file_size)
                
                # If no matching read file, use a heuristic:
                # If file_size is small compared to typical media, it's probably still growing
                # Use max of current size * 2 as estimate (will adjust as it grows)
                if target_size == file_size and file_size < 10 * 1024 * 1024 * 1024:  # < 10GB
                    # No source found, we'll track size growth
                    target_size = file_size if file_size > 0 else 1
                
                key = f"{fd}_{filepath}"
                open_files[key] = FileTransferInfo(fd, str(filepath), current_pos, file_size, target_size)
        
        return open_files
    
    except (PermissionError, OSError):
        return {}

def select_process_interactive():
    """Interactive process selection"""
    processes = find_arr_processes()
    
    if not processes:
        print("No *arr processes found running.")
        print(f"\nAvailable managers: {', '.join(ARR_MANAGERS)}")
        print("\nYou can also monitor a specific PID:")
        print("  arr-monitor.py <PID>")
        return None
    
    if len(processes) == 1:
        pid, name = processes[0]
        print(f"Found: {name} (PID: {pid})")
        return pid
    
    print(f"Found {len(processes)} *arr process(es):\n")
    for i, (pid, name) in enumerate(processes, 1):
        print(f"  {i}. {name} (PID: {pid})")
    
    while True:
        try:
            choice = input(f"\nSelect process to monitor [1-{len(processes)}]: ")
            idx = int(choice) - 1
            if 0 <= idx < len(processes):
                return processes[idx][0]
            print("Invalid selection")
        except (ValueError, KeyboardInterrupt):
            return None

def draw_ui(stdscr, pid, files, last_update):
    """Draw the curses UI"""
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    
    # Header
    try:
        proc = psutil.Process(pid)
        proc_name = proc.name()
    except:
        proc_name = "Unknown"
    
    header = f"*arr File Transfer Monitor - PID: {pid} ({proc_name})"
    stdscr.addstr(0, 0, header[:width-1], curses.A_BOLD | curses.color_pair(1))
    stdscr.addstr(1, 0, f"Time: {datetime.now().strftime('%H:%M:%S')}", curses.color_pair(2))
    stdscr.addstr(2, 0, "─" * min(width - 1, 80))
    
    if not files:
        stdscr.addstr(4, 0, "No active file writes detected...", curses.color_pair(3))
        stdscr.addstr(height - 1, 0, "Press 'q' to quit", curses.color_pair(2))
        stdscr.refresh()
        return
    
    # File information
    row = 4
    for key, file_info in files.items():
        if row >= height - 3:
            break
        
        # Filename
        filename = file_info.filename
        if len(filename) > width - 1:
            filename = "..." + filename[-(width-4):]
        stdscr.addstr(row, 0, filename, curses.A_BOLD | curses.color_pair(4))
        row += 1
        
        # Filepath
        filepath = f"  {file_info.filepath}"
        if len(filepath) > width - 1:
            filepath = filepath[:width-4] + "..."
        stdscr.addstr(row, 0, filepath, curses.color_pair(5))
        row += 1
        
        # Progress bar
        bar_width = min(40, width - 20)
        filled = int((file_info.percent / 100) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        progress_str = f"  [{bar}] {file_info.percent:.1f}%"
        stdscr.addstr(row, 0, progress_str, curses.color_pair(6))
        row += 1
        
        # Size and speed info
        size_str = f"  {format_size(file_info.position)} / {format_size(file_info.target_size)}"
        stdscr.addstr(row, 0, size_str, curses.color_pair(2))
        
        if file_info.speed > 0:
            speed_str = f"  Speed: {format_speed(file_info.speed)}"
            eta_str = f"  ETA: {format_time(file_info.eta_seconds)}"
            info = speed_str + eta_str
            if len(size_str) + len(info) < width - 1:
                stdscr.addstr(row, len(size_str), info, curses.color_pair(7))
        
        row += 2
        
        if row >= height - 2:
            break
    
    # Footer
    stdscr.addstr(height - 1, 0, "Press 'q' to quit", curses.color_pair(2))
    stdscr.refresh()

def monitor_with_curses(stdscr, pid):
    """Main monitoring loop with curses UI"""
    # Setup colors
    curses.start_color()
    curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(6, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    curses.init_pair(7, curses.COLOR_CYAN, curses.COLOR_BLACK)
    
    # Non-blocking input
    stdscr.nodelay(True)
    curses.curs_set(0)
    
    tracked_files = {}
    last_update = time.time()
    
    while True:
        try:
            # Check for quit key
            key = stdscr.getch()
            if key == ord('q') or key == ord('Q'):
                break
            
            # Check if process still exists
            if not psutil.pid_exists(pid):
                stdscr.clear()
                stdscr.addstr(0, 0, f"Process {pid} has exited.", curses.A_BOLD)
                stdscr.addstr(1, 0, "Press any key to exit...")
                stdscr.nodelay(False)
                stdscr.getch()
                break
            
            # Get current open files
            current_files = get_open_files(pid)
            
            # Update tracked files
            for key, file_info in current_files.items():
                if key in tracked_files:
                    tracked_files[key].update(file_info.position, file_info.size)
                else:
                    tracked_files[key] = file_info
            
            # Remove files that are no longer open
            keys_to_remove = [k for k in tracked_files if k not in current_files]
            for key in keys_to_remove:
                del tracked_files[key]
            
            # Draw UI
            draw_ui(stdscr, pid, tracked_files, last_update)
            last_update = time.time()
            
            time.sleep(0.5)  # Faster refresh - 500ms
        
        except KeyboardInterrupt:
            break
        except curses.error:
            # Window too small or other curses error
            time.sleep(0.1)
            continue

def main():
    parser = argparse.ArgumentParser(
        description='Monitor file write operations for *arr media managers'
    )
    parser.add_argument('pid', type=int, nargs='?', 
                       help='Process ID to monitor')
    parser.add_argument('-d', '--debug', action='store_true',
                       help='Show debug information')
    
    args = parser.parse_args()
    
    # Determine PID to monitor
    if args.pid:
        pid = args.pid
        if not psutil.pid_exists(pid):
            print(f"Error: Process {pid} does not exist")
            return 1
        
        try:
            proc = psutil.Process(pid)
            print(f"Monitoring: {proc.name()} (PID: {pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print(f"Monitoring PID: {pid}")
    else:
        pid = select_process_interactive()
        if pid is None:
            return 1
    
    # Debug mode - print what we find and exit
    if args.debug:
        print(f"\nDebug: Scanning /proc/{pid}/fd/...")
        files = get_open_files(pid)
        if not files:
            print("No files found matching criteria")
            
            # Show all FDs for debugging
            print("\nAll open file descriptors:")
            fd_dir = Path(f"/proc/{pid}/fd")
            for fd_link in fd_dir.iterdir():
                try:
                    filepath = fd_link.resolve()
                    fdinfo_path = Path(f"/proc/{pid}/fdinfo/{fd_link.name}")
                    
                    if fdinfo_path.exists():
                        with open(fdinfo_path, 'r') as f:
                            lines = f.read()
                        print(f"\nFD {fd_link.name}: {filepath}")
                        print(f"  fdinfo: {lines[:200]}")
                except:
                    pass
        else:
            print(f"\nFound {len(files)} file(s) being written:")
            for key, info in files.items():
                print(f"\n  FD {info.fd}: {info.filepath}")
                print(f"    Position: {info.position}, Size: {info.size}")
                print(f"    Percent: {info.percent:.1f}%")
        return 0
    
    # Check for root/sudo if needed
    try:
        test_dir = Path(f"/proc/{pid}/fd")
        list(test_dir.iterdir())
    except PermissionError:
        print("\nError: Permission denied. Try running with sudo:")
        print(f"  sudo {sys.argv[0]} {pid}")
        return 1
    
    # Start curses interface
    try:
        curses.wrapper(monitor_with_curses, pid)
    except KeyboardInterrupt:
        pass
    
    return 0

if __name__ == '__main__':
    sys.exit(main())