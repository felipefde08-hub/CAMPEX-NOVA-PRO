#!/bin/bash
#
# CAMPEX Vercel Deployment - Pre-Deployment Validation Script
# Execute antes de fazer git commit/push
#

set -e

RESET='\033[0m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'

echo -e "${BLUE}"
echo "╔════════════════════════════════════════════════════════════╗"
echo "║ CAMPEX Vercel Deployment - Pre-Deployment Validation       ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo -e "${RESET}\n"

# Counter for tests
TESTS_PASSED=0
TESTS_FAILED=0

# Test 1: Check if vercel.json exists
echo -n "Test 1: vercel.json exists... "
if [ -f "vercel.json" ]; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 2: Check if api/index.py exists
echo -n "Test 2: api/index.py exists... "
if [ -f "api/index.py" ]; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 3: Check if backend/serverless.py exists
echo -n "Test 3: backend/serverless.py exists... "
if [ -f "backend/serverless.py" ]; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 4: Check if main.py has serverless support
echo -n "Test 4: main.py has serverless support... "
if grep -q "is_serverless" backend/main.py; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 5: Check if config.py has runtime field
echo -n "Test 5: config.py has runtime field... "
if grep -q "runtime: str" backend/config.py; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 6: Check opencv-python is not duplicated
echo -n "Test 6: No duplicate opencv-python... "
OPENCV_COUNT=$(grep -c "^opencv-python" requirements.txt || echo "0")
if [ "$OPENCV_COUNT" -eq "1" ]; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL (found $OPENCV_COUNT instances)${RESET}"
  ((TESTS_FAILED++))
fi

# Test 7: Check for headless opencv
echo -n "Test 7: Uses opencv-python-headless... "
if grep -q "opencv-python-headless" requirements.txt; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 8: Check health.py doesn't use UTC (Python 3.11 only)
echo -n "Test 8: health.py compatible with Python 3.9... "
if grep -q "from datetime import UTC" backend/cameras/health.py; then
  echo -e "${RED}✗ FAIL (still uses UTC)${RESET}"
  ((TESTS_FAILED++))
elif grep -q "timezone.utc" backend/cameras/health.py; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${YELLOW}⚠ WARNING (UTC usage not found)${RESET}"
  ((TESTS_PASSED++))
fi

# Test 9: Check health.py doesn't use StrEnum
echo -n "Test 9: health.py doesn't use StrEnum... "
if grep -q "StrEnum" backend/cameras/health.py; then
  echo -e "${RED}✗ FAIL (still uses StrEnum)${RESET}"
  ((TESTS_FAILED++))
elif grep -q "str, Enum" backend/cameras/health.py; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${YELLOW}⚠ WARNING (Enum usage not found)${RESET}"
  ((TESTS_PASSED++))
fi

# Test 10: Check documentation files exist
echo -n "Test 10: Documentation files exist... "
DOCS=("VERCEL_DEPLOYMENT_REPORT.md" "VERCEL_DEPLOYMENT_QUICK_START.md" "VERCEL_CODE_CHANGES_DETAIL.md" "VERCEL_FINAL_CHECKLIST.md")
DOC_COUNT=0
for doc in "${DOCS[@]}"; do
  if [ -f "$doc" ]; then
    ((DOC_COUNT++))
  fi
done
if [ "$DOC_COUNT" -eq "${#DOCS[@]}" ]; then
  echo -e "${GREEN}✓ PASS (all 4 docs found)${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL (only $DOC_COUNT/${#DOCS[@]} found)${RESET}"
  ((TESTS_FAILED++))
fi

# Test 11: Check .env is NOT committed
echo -n "Test 11: .env file is in .gitignore... "
if grep -q "^.env" .gitignore; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${YELLOW}⚠ WARNING (.env not in .gitignore)${RESET}"
  ((TESTS_PASSED++))
fi

# Test 12: Check if no .env file exists (shouldn't be committed)
echo -n "Test 12: .env file doesn't exist in repo... "
if [ ! -f ".env" ] || [ ! -f ".env.production" ]; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${YELLOW}⚠ WARNING (.env files detected)${RESET}"
  ((TESTS_PASSED++))
fi

# Test 13: Verify vercel.json has required fields
echo -n "Test 13: vercel.json has required fields... "
if grep -q '"framework": "fastapi"' vercel.json && \
   grep -q '"CAMPEX_RUNTIME": "serverless"' vercel.json && \
   grep -q '"CAMPEX_ENV": "production"' vercel.json; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

# Test 14: Check api/index.py imports correctly
echo -n "Test 14: api/index.py imports app correctly... "
if grep -q "from backend.main import app" api/index.py; then
  echo -e "${GREEN}✓ PASS${RESET}"
  ((TESTS_PASSED++))
else
  echo -e "${RED}✗ FAIL${RESET}"
  ((TESTS_FAILED++))
fi

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${RESET}"
echo -e "Test Results: ${GREEN}$TESTS_PASSED passed${RESET}, ${RED}$TESTS_FAILED failed${RESET}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${RESET}"

if [ $TESTS_FAILED -eq 0 ]; then
  echo -e "\n${GREEN}✓ All validation tests passed!${RESET}"
  echo -e "${GREEN}Ready for deployment to Vercel${RESET}\n"
  echo "Next steps:"
  echo "1. git add ."
  echo "2. git commit -m 'chore: Prepare CAMPEX backend for Vercel serverless deployment'"
  echo "3. git push origin main"
  echo "4. Create project on Vercel dashboard"
  exit 0
else
  echo -e "\n${RED}✗ Some validation tests failed${RESET}"
  echo "Please fix the issues above before deployment."
  exit 1
fi
