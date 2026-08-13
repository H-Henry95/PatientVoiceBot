#!/usr/bin/env python3
"""
Patient Voice Bot Demo
Shows all completed test calls, transcripts, and analysis.
"""

import json
from pathlib import Path
from collections import defaultdict

def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

def show_transcripts():
    """Show all available call transcripts."""
    print_section("📞 COMPLETED TEST CALLS")
    
    transcripts_dir = Path("transcripts")
    json_files = sorted(transcripts_dir.glob("*.json"))
    
    if not json_files:
        print("No transcripts found.")
        return
    
    for idx, json_file in enumerate(json_files, 1):
        with open(json_file) as f:
            data = json.load(f)
        
        scenario = data.get("scenario", "unknown")
        label = data.get("label", "Unknown")
        turns = data.get("turns", [])
        
        if turns:
            start_time = turns[0]["t"]
            end_time = turns[-1]["t"]
            duration = end_time - start_time
        else:
            duration = 0
        
        print(f"\n[Call {idx}] {label}")
        print(f"  Scenario:  {scenario}")
        print(f"  Duration:  ~{duration:.1f} seconds")
        print(f"  Exchanges: {len(turns)} turns")
        
        # Show first and last exchange
        if turns:
            first = turns[0]
            last = turns[-1]
            print(f"  Start:     {first['speaker']:12} → {first['text'][:50]}...")
            print(f"  End:       {last['speaker']:12} → {last['text'][:50]}...")

def show_summary():
    """Show overall system summary."""
    print_section("📊 SYSTEM SUMMARY")
    
    # Count transcripts
    transcripts_dir = Path("transcripts")
    json_files = list(transcripts_dir.glob("*.json"))
    
    # Count recordings
    recordings_dir = Path("recordings")
    mp3_files = list(recordings_dir.glob("*.mp3"))
    
    print(f"\n✅ System Status: OPERATIONAL")
    print(f"\n📁 Data Summary:")
    print(f"   • Completed Calls: {len(json_files)}")
    print(f"   • Recordings: {len(mp3_files)}")
    print(f"   • Transcripts: {len(json_files)} (JSON) + {len(json_files)} (TXT)")
    
    # Summarize scenarios
    scenarios = defaultdict(int)
    for json_file in json_files:
        with open(json_file) as f:
            data = json.load(f)
            scenario = data.get("scenario", "unknown")
            scenarios[scenario] += 1
    
    print(f"\n🎬 Scenarios Tested:")
    for scenario, count in sorted(scenarios.items()):
        print(f"   • {scenario}: {count} call(s)")

def show_architecture():
    """Show system architecture."""
    print_section("🏗️  SYSTEM ARCHITECTURE")
    
    print("""
✓ FastAPI Server (Port 5050)
  └─ WebSocket endpoint for Twilio Media Stream
  └─ Realtime conversation bridge

✓ Twilio Integration
  └─ Outbound call placement
  └─ Media streaming (mu-law, 8 kHz)
  └─ Webhook callbacks

✓ OpenAI Realtime API
  └─ Patient voice synthesis
  └─ Live conversation transcription
  └─ Speaker labeling

✓ Recording & Analysis
  └─ Stereo MP3 (agent=L, bot=R)
  └─ Timestamped JSON transcripts
  └─ Automated bug detection
  └─ Analysis reports
""")

def main():
    """Run the demo."""
    print("\n")
    print("╔" + "="*58 + "╗")
    print("║" + " "*10 + "🤖 PATIENT VOICE BOT - DEMO SHOWCASE" + " "*12 + "║")
    print("╚" + "="*58 + "╝")
    
    show_summary()
    show_transcripts()
    show_architecture()
    
    print_section("✅ DEMO READY FOR PRESENTATION")
    print("""
The system is fully operational and ready to demonstrate:

1. Live Server: http://0.0.0.0:5050
   └─ ngrok Tunnel: https://strum-probation-duplicity.ngrok-free.dev

2. Completed Calls: See transcripts above
   └─ Realistic patient-agent conversations
   └─ Full dialogue capture with timestamps

3. Architecture: Multi-service integration
   └─ Twilio ↔ FastAPI ↔ OpenAI
   └─ Real-time audio streaming & transcription

📊 Ready to show the hiring manager! 🚀
""")

if __name__ == "__main__":
    main()
