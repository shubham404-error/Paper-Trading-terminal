# -*- coding: utf-8 -*-
import re
with open('app.py', 'r', encoding='utf-8', errors='ignore') as f:
    content = f.read()

content = re.sub(r'#### .*Core Capabilities', '#### ⚙️ Core Capabilities', content)
content = re.sub(r'#### .*Navigation & Inputs', '#### 🧭 Navigation & Inputs', content)
content = re.sub(r'#### .*Trading Best Practices', '#### 💡 Trading Best Practices', content)
content = re.sub(r'with st\.expander\(".*Frequently Asked Questions", expanded=False\):', 'with st.expander("❓ Frequently Asked Questions", expanded=False):', content)

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)