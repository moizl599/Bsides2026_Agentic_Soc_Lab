#!/bin/bash
# Push BSides 2026 Agentic SOC Lab to GitHub
# Run this script from the repo folder:
#   cd ~/Desktop/Bsides2026_Agentic_Soc_Lab && bash push_to_github.sh

set -e

git init
git add .
git commit -m "Initial commit: Agentic SOC Lab + BSides Toronto presentation"
git branch -M main
git remote add origin https://github.com/moizl599/Bsides2026_Agentic_Soc_Lab.git
git push -u origin main

echo ""
echo "✅ Pushed to https://github.com/moizl599/Bsides2026_Agentic_Soc_Lab"

# Clean up this script (optional — it's not needed in the repo)
# rm -- "$0"
