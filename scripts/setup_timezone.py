#!/usr/bin/env python3
"""
Timezone Synchronization Script for MT5 Bot
Sets VPS timezone to UTC+2 (MetaQuotes time) and configures NTP sync
"""

import os
import sys
import subprocess
import logging
from datetime import datetime

def setup_logging():
    """Setup logging for timezone setup"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('timezone_setup.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

def run_command(command, description, shell=True):
    """Run command and log results"""
    logger = logging.getLogger(__name__)
    logger.info(f"Executing: {description}")
    logger.info(f"Command: {command}")
    
    try:
        if shell:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        else:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        
        logger.info(f"Return code: {result.returncode}")
        if result.stdout:
            logger.info(f"STDOUT: {result.stdout.strip()}")
        if result.stderr:
            logger.warning(f"STDERR: {result.stderr.strip()}")
        
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out: {command}")
        return False, "", "Command timed out"
    except Exception as e:
        logger.error(f"Command failed: {e}")
        return False, "", str(e)

def check_admin_privileges():
    """Check if running with admin privileges"""
    logger = logging.getLogger(__name__)
    logger.info("Checking admin privileges...")
    
    try:
        # Try to run a command that requires admin rights
        success, _, _ = run_command('net session', 'Check admin privileges')
        if success:
            logger.info("✅ Running with admin privileges")
            return True
        else:
            logger.error("❌ Admin privileges required")
            return False
    except Exception as e:
        logger.error(f"Error checking admin privileges: {e}")
        return False

def set_timezone_utc_plus_2():
    """Set system timezone to UTC+2 (E. Europe Standard Time)"""
    logger = logging.getLogger(__name__)
    logger.info("Setting timezone to UTC+2 (E. Europe Standard Time)...")
    
    # Set timezone
    success, stdout, stderr = run_command(
        'tzutil /s "E. Europe Standard Time"',
        'Set timezone to UTC+2'
    )
    
    if success:
        logger.info("✅ Timezone set to UTC+2")
        return True
    else:
        logger.error(f"❌ Failed to set timezone: {stderr}")
        return False

def verify_timezone():
    """Verify current timezone setting"""
    logger = logging.getLogger(__name__)
    logger.info("Verifying current timezone...")
    
    success, stdout, stderr = run_command('tzutil /g', 'Get current timezone')
    
    if success:
        current_tz = stdout.strip()
        logger.info(f"Current timezone: {current_tz}")
        
        # Check if it's UTC+2
        utc_plus_2_zones = [
            "E. Europe Standard Time",
            "GTB Standard Time",  # Alternative UTC+2
            "FLE Standard Time"   # Alternative UTC+2
        ]
        
        if any(zone in current_tz for zone in utc_plus_2_zones):
            logger.info("✅ Timezone verified as UTC+2")
            return True
        else:
            logger.warning(f"⚠️ Timezone may not be UTC+2: {current_tz}")
            return False
    else:
        logger.error(f"❌ Failed to get timezone: {stderr}")
        return False

def configure_ntp_sync():
    """Configure NTP time synchronization"""
    logger = logging.getLogger(__name__)
    logger.info("Configuring NTP time synchronization...")
    
    # Configure Windows Time service
    commands = [
        ('w32tm /config /manualpeerlist:"time.windows.com,0x8" /syncfromflags:manual /reliable:no /update', 'Configure NTP servers'),
        ('net stop w32time', 'Stop Windows Time service'),
        ('net start w32time', 'Start Windows Time service'),
        ('w32tm /resync /force', 'Force time synchronization'),
        ('w32tm /query /status', 'Check time service status')
    ]
    
    all_success = True
    for cmd, desc in commands:
        success, stdout, stderr = run_command(cmd, desc)
        if not success:
            logger.error(f"❌ Failed: {desc} - {stderr}")
            all_success = False
        else:
            logger.info(f"✅ Completed: {desc}")
    
    return all_success

def verify_time_sync():
    """Verify time synchronization"""
    logger = logging.getLogger(__name__)
    logger.info("Verifying time synchronization...")
    
    # Get current system time
    success, stdout, stderr = run_command('powershell "Get-Date -Format \\"yyyy-MM-dd HH:mm:ss\\""', 'Get system time')
    
    if success:
        system_time = stdout.strip()
        logger.info(f"System time: {system_time}")
        
        # Get NTP time (simple check)
        success, stdout, stderr = run_command('w32tm /query /source', 'Get time source')
        if success:
            time_source = stdout.strip()
            logger.info(f"Time source: {time_source}")
            
            if "time.windows.com" in time_source or "Microsoft" in time_source:
                logger.info("✅ Time synchronization verified")
                return True
            else:
                logger.warning(f"⚠️ Unexpected time source: {time_source}")
                return False
        else:
            logger.error(f"❌ Failed to get time source: {stderr}")
            return False
    else:
        logger.error(f"❌ Failed to get system time: {stderr}")
        return False

def create_startup_script():
    """Create startup script for automatic timezone sync"""
    logger = logging.getLogger(__name__)
    logger.info("Creating startup script for automatic timezone sync...")
    
    script_content = '''@echo off
REM MT5 Bot Timezone Sync Script
REM Ensures VPS stays synchronized with UTC+2

echo Starting timezone synchronization...
tzutil /s "E. Europe Standard Time"
net stop w32time
net start w32time
w32tm /resync /force
echo Timezone synchronization complete

REM Verify timezone
tzutil /g
REM Verify time sync
w32tm /query /status
'''
    
    script_path = os.path.join(os.getcwd(), 'sync_timezone.bat')
    
    try:
        with open(script_path, 'w') as f:
            f.write(script_content)
        
        logger.info(f"✅ Startup script created: {script_path}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to create startup script: {e}")
        return False

def setup_scheduled_task():
    """Setup scheduled task for daily timezone sync"""
    logger = logging.getLogger(__name__)
    logger.info("Setting up scheduled task for daily timezone sync...")
    
    script_path = os.path.join(os.getcwd(), 'sync_timezone.bat')
    
    # Create scheduled task (runs daily at 3 AM)
    command = f'''schtasks /create /tn "MT5Bot_TimezoneSync" /tr "{script_path}" /sc daily /st 03:00 /ru SYSTEM /f'''
    
    success, stdout, stderr = run_command(command, 'Create scheduled task')
    
    if success:
        logger.info("✅ Scheduled task created for daily timezone sync")
        return True
    else:
        logger.error(f"❌ Failed to create scheduled task: {stderr}")
        return False

def display_final_status():
    """Display final setup status"""
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("TIMEZONE SETUP COMPLETE")
    logger.info("=" * 60)
    
    # Show current time and timezone
    success, stdout, stderr = run_command('tzutil /g', 'Get timezone')
    if success:
        logger.info(f"Timezone: {stdout.strip()}")
    
    success, stdout, stderr = run_command('powershell "Get-Date -Format \\"yyyy-MM-dd HH:mm:ss UTC\\\""', 'Get UTC time')
    if success:
        logger.info(f"UTC Time: {stdout.strip()}")
    
    success, stdout, stderr = run_command('powershell "Get-Date -Format \\"yyyy-MM-dd HH:mm:ss Local\\\""', 'Get local time')
    if success:
        logger.info(f"Local Time: {stdout.strip()}")
    
    logger.info("=" * 60)
    logger.info("MetaQuotes Server Time: UTC+2")
    logger.info("Your VPS should now be synchronized with MT5 server time")
    logger.info("=" * 60)

def main():
    """Main timezone setup function"""
    logger = setup_logging()
    logger.info("Starting MT5 Bot Timezone Setup")
    logger.info("Target: UTC+2 (MetaQuotes Server Time)")
    
    # Check admin privileges
    if not check_admin_privileges():
        logger.error("Please run this script as Administrator")
        return False
    
    # Step 1: Set timezone
    if not set_timezone_utc_plus_2():
        logger.error("Failed to set timezone")
        return False
    
    # Step 2: Verify timezone
    if not verify_timezone():
        logger.error("Timezone verification failed")
        return False
    
    # Step 3: Configure NTP sync
    if not configure_ntp_sync():
        logger.error("Failed to configure NTP sync")
        return False
    
    # Step 4: Verify time sync
    if not verify_time_sync():
        logger.error("Time sync verification failed")
        return False
    
    # Step 5: Create startup script
    if not create_startup_script():
        logger.error("Failed to create startup script")
        return False
    
    # Step 6: Setup scheduled task
    if not setup_scheduled_task():
        logger.error("Failed to setup scheduled task")
        return False
    
    # Display final status
    display_final_status()
    
    logger.info("✅ Timezone setup completed successfully!")
    logger.info("Your VPS is now synchronized with MT5 server time (UTC+2)")
    
    return True

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Setup interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Setup failed: {e}")
        sys.exit(1)
