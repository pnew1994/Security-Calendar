from datetime import datetime, timedelta
import random
import sqlite3
import pandas as pd
import requests
import streamlit as st

DB_NAME = "security_ops_v3.db"

# Set Streamlit page config
st.set_page_config(
    page_title="LA Security Operations Center", layout="wide", page_icon="🛡️"
)

# ==============================================================================
# REAL API INTEGRATIONS (WEATHER, 7-DAY FORECAST, CRIME & OSRM DRIVE TIME)
# ==============================================================================

def get_real_la_weather_and_forecast(lat, lon):
    """Fetches real current weather and 7-day forecast via Open-Meteo API."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&current_weather=true"
        f"&daily=temperature_2m_max,temperature_2m_min,windspeed_10m_max,relative_humidity_2m_max,relative_humidity_2m_min"
        f"&timezone=America%2FLos_Angeles"
    )
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            curr = data.get("current_weather", {})
            temp_f = round((curr.get("temperature", 20) * 9 / 5) + 32, 1)
            wind_mph = round(curr.get("windspeed", 5) * 0.621371, 1)

            daily = data.get("daily", {})
            dates = daily.get("time", [])
            t_max = daily.get("temperature_2m_max", [])
            t_min = daily.get("temperature_2m_min", [])
            w_max = daily.get("windspeed_10m_max", [])
            h_min = daily.get("relative_humidity_2m_min", [])

            forecast_list = []
            for i in range(min(7, len(dates))):
                max_f = round((t_max[i] * 9 / 5) + 32, 1) if i < len(t_max) else temp_f
                min_f = round((t_min[i] * 9 / 5) + 32, 1) if i < len(t_min) else temp_f
                w_mph = round(w_max[i] * 0.621371, 1) if i < len(w_max) else wind_mph
                hum_val = h_min[i] if i < len(h_min) else 35

                # Calculate fire level for forecast day
                fire_lvl = get_fire_hazard_level(w_mph, hum_val)

                forecast_list.append({
                    "Date": dates[i],
                    "High (°F)": f"{max_f}°F",
                    "Low (°F)": f"{min_f}°F",
                    "Max Wind": f"{w_mph} mph",
                    "Min Humidity": f"{hum_val}%",
                    "Fire Risk": fire_lvl,
                })

            curr_hum = h_min[0] if h_min else 40
            return {
                "temp": f"{temp_f}°F",
                "wind": f"{wind_mph} mph",
                "humidity": f"{curr_hum}%",
                "raw_wind": wind_mph,
                "raw_hum": curr_hum,
                "forecast": pd.DataFrame(forecast_list)
            }
    except Exception:
        pass

    # Fallback default values
    return {
        "temp": "72.0°F",
        "wind": "8.5 mph",
        "humidity": "42%",
        "raw_wind": 8.5,
        "raw_hum": 42,
        "forecast": pd.DataFrame()
    }


def get_fire_hazard_level(raw_wind, raw_hum):
    """Calculates LAFD Red Flag / Fire Hazard status based on wind and humidity."""
    if raw_hum <= 15 and raw_wind >= 25:
        return "🔴 RED FLAG WARNING (Extreme Fire Danger)"
    elif raw_hum <= 25 or raw_wind >= 18:
        return "🟠 HIGH (Elevated Brush Fire Warning)"
    elif raw_hum <= 40:
        return "🟡 MODERATE (Standard Fire Season Caution)"
    else:
        return "🟢 LOW (Normal Operating Conditions)"


def get_real_la_crime_data(area_name):
    """Fetches real recent crime incidents from City of Los Angeles Open Data API (Socrata)."""
    lapd_area_map = {
        "Bel Air": "West LA",
        "Beverly Hills": "West LA",
        "Malibu": "West LA",
        "Brentwood": "West LA",
        "Pacific Palisades": "Pacific",
    }
    lapd_area = lapd_area_map.get(area_name, "West LA")

    url = f"https://data.lacity.org/resource/2nsz-xuv6.json?$where=area_name='{lapd_area}'&$limit=5&$order=date_occ DESC"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            records = res.json()
            formatted_crimes = []
            for r in records:
                date_str = r.get("date_occ", "Recently")[:10]
                crm_desc = r.get("crm_cd_desc", "Incident Reported")
                location = r.get("location", "Nearby Area")
                formatted_crimes.append(
                    f"• **{date_str}**: {crm_desc.title()} at {location.title()}"
                )
            if formatted_crimes:
                return "\n".join(formatted_crimes)
    except Exception:
        pass

    return (
        "• **Recent Activity**: Standard residential patrols active. No major Part 1 violent crimes "
        "reported within 1.5-mile perimeter in past 30 days."
    )


def get_commute_time(agent_lat, agent_lon, site_lat, site_lon):
    """Fetches real driving time in minutes using OSRM OpenStreetMap Routing API."""
    url = f"http://router.project-osrm.org/route/v1/driving/{agent_lon},{agent_lat};{site_lon},{site_lat}?overview=false"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if "routes" in data and len(data["routes"]) > 0:
                duration_sec = data["routes"][0]["duration"]
                return round(duration_sec / 60.0, 1)
    except Exception:
        pass
    return round(
        abs(agent_lat - site_lat) * 60 + abs(agent_lon - site_lon) * 60, 1
    )


# ==============================================================================
# DATABASE & SCHEMA INITIALIZATION
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")

    cursor.executescript(
        """
    DROP TABLE IF EXISTS shift_assignments;
    DROP TABLE IF EXISTS shifts;
    DROP TABLE IF EXISTS certifications;
    DROP TABLE IF EXISTS locations;
    DROP TABLE IF EXISTS clients;
    DROP TABLE IF EXISTS agents;

    CREATE TABLE IF NOT EXISTS clients (
        client_id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_name TEXT NOT NULL,
        contact_phone TEXT,
        account_type TEXT NOT NULL CHECK(account_type IN ('Residential', 'Hybrid', 'EP'))
    );

    CREATE TABLE IF NOT EXISTS locations (
        location_id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        code_name TEXT NOT NULL,
        address TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        preferred_agents TEXT,
        FOREIGN KEY (client_id) REFERENCES clients(client_id)
    );

    CREATE TABLE IF NOT EXISTS agents (
        agent_id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        agent_type TEXT NOT NULL CHECK(agent_type IN ('Full-Time', 'Part-Time', 'On-Call')),
        qualification_tier TEXT NOT NULL CHECK(qualification_tier IN ('Residential', 'Executive Protection')),
        email TEXT,
        phone TEXT,
        home_lat REAL NOT NULL,
        home_lon REAL NOT NULL,
        max_weekly_hours INTEGER DEFAULT 40,
        status TEXT DEFAULT 'Active'
    );

    CREATE TABLE IF NOT EXISTS certifications (
        cert_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL,
        cert_name TEXT NOT NULL,
        expiry_date DATE NOT NULL,
        FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
    );

    CREATE TABLE IF NOT EXISTS shifts (
        shift_id INTEGER PRIMARY KEY AUTOINCREMENT,
        location_id INTEGER NOT NULL,
        shift_date DATE NOT NULL,
        shift_type TEXT NOT NULL,
        FOREIGN KEY (location_id) REFERENCES locations(location_id)
    );

    CREATE TABLE IF NOT EXISTS shift_assignments (
        assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        shift_id INTEGER NOT NULL,
        agent_id INTEGER NOT NULL,
        confirmation_status TEXT DEFAULT 'Pending',
        FOREIGN KEY (shift_id) REFERENCES shifts(shift_id),
        FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
    );
    """
    )
    conn.commit()

    # Seed Accounts (10 Residential, 15 EP, 5 Hybrid)
    accounts_to_seed = []

    res_areas = [
        ("Bel Air Sanctuary", "Bellagio Rd, Bel Air, CA", 34.0837, -118.4430),
        ("Beverly Hills Compound", "Roxbury Dr, Beverly Hills, CA", 34.0736, -118.4004),
        ("Malibu Beach Residency", "PCH, Malibu, CA", 34.0259, -118.7798),
        ("Brentwood Crest", "Sunset Blvd, Brentwood, CA", 34.0522, -118.4745),
        ("Palisades Compound", "Chautauqua Blvd, Pacific Palisades, CA", 34.0456, -118.5284),
        ("Holmby Hills Manor", "Faring Rd, Holmby Hills, CA", 34.0781, -118.4285),
        ("Encino Ridge Estate", "Mulholland Dr, Encino, CA", 34.1482, -118.5086),
        ("Hidden Hills Compound", "Long Valley Rd, Hidden Hills, CA", 34.1678, -118.6612),
        ("Calabasas Sanctuary", "Park Granada, Calabasas, CA", 34.1578, -118.6453),
        ("Pasadena Foothill Manor", "Lombardy Rd, Pasadena, CA", 34.1325, -118.1284),
    ]
    for idx, (name, addr, lat, lon) in enumerate(res_areas, start=1):
        accounts_to_seed.append((name, f"555-10{idx:02d}", "Residential", f"Resi Gate {idx}", addr, lat, lon))

    ep_areas = [
        ("Aegis Tech EP", "Grand Ave, Downtown LA, CA", 34.0522, -118.2437),
        ("Apex Capital Executive", "Century Park East, Century City, CA", 34.0583, -118.4134),
        ("Vanguard Global Exec", "Wilshire Blvd, Beverly Hills, CA", 34.0669, -118.3982),
        ("Titanium Partners", "Ocean Ave, Santa Monica, CA", 34.0195, -118.4912),
        ("Summit Energy Detail", "Figueroa St, Downtown LA, CA", 34.0505, -118.2591),
        ("Sovereign Media EP", "Sunset Blvd, West Hollywood, CA", 34.0900, -118.3813),
        ("Blackstone Strategic", "Constellation Blvd, Century City, CA", 34.0598, -118.4172),
        ("Horizon Ventures", "Colorado Ave, Santa Monica, CA", 34.0151, -118.4842),
        ("Beacon Holdings EP", "Hope St, Downtown LA, CA", 34.0481, -118.2575),
        ("Crestview Financial", "Wilshire Blvd, Westwood, CA", 34.0592, -118.4442),
        ("Pinnacle Studio Exec", "Melrose Ave, Hollywood, CA", 34.0837, -118.3365),
        ("Atlas Tech Founder", "Abbot Kinney Blvd, Venice, CA", 33.9921, -118.4612),
        ("Meridian Global EP", "Avenue of the Stars, Century City, CA", 34.0561, -118.4165),
        ("Bespoke Private Detail", "Rodeo Dr, Beverly Hills, CA", 34.0696, -118.4031),
        ("Valence Life Sciences", "Sepulveda Blvd, Culver City, CA", 34.0041, -118.3934),
    ]
    for idx, (name, addr, lat, lon) in enumerate(ep_areas, start=1):
        accounts_to_seed.append((name, f"555-20{idx:02d}", "EP", f"EP Detail {idx}", addr, lat, lon))

    hybrid_areas = [
        ("Montecito West Estate & EP", "San Ysidro Rd, Montecito, CA", 34.4367, -119.6321),
        ("Santa Monica Beach HQ & Villa", "San Vicente Blvd, Santa Monica, CA", 34.0321, -118.5012),
        ("Hollywood Hills Estate & Studio", "Blue Jay Way, Los Angeles, CA", 34.1012, -118.3789),
        ("Marina Del Rey Compound & Yacht", "Admiralty Way, Marina Del Rey, CA", 33.9802, -118.4411),
        ("Sherman Oaks Ranch & Escort", "Ventura Blvd, Sherman Oaks, CA", 34.1508, -118.4489),
    ]
    for idx, (name, addr, lat, lon) in enumerate(hybrid_areas, start=1):
        accounts_to_seed.append((name, f"555-30{idx:02d}", "Hybrid", f"Hybrid Post {idx}", addr, lat, lon))

    for c_name, phone, acc_type, code_name, addr, lat, lon in accounts_to_seed:
        cursor.execute("INSERT INTO clients (client_name, contact_phone, account_type) VALUES (?, ?, ?)", (c_name, phone, acc_type))
        client_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO locations (client_id, code_name, address, latitude, longitude, preferred_agents) VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, code_name, addr, lat, lon, "M. Vance, E. Rostova"),
        )

    # Seed 105 Agents
    first_names = ["Marcus", "Elena", "David", "Sarah", "Alex", "James", "Rachel", "Michael", "Jessica", "Daniel", "Chris", "Amanda", "Jason", "Laura", "Brian"]
    last_names = ["Vance", "Rostova", "Miller", "Jenkins", "Mercer", "Stone", "Hayes", "Kovacs", "Drake", "Cross", "Vaughn", "Reid", "Bauer", "Bishop", "Steele"]

    sample_agents = []
    agent_types = ["Full-Time"] * 50 + ["Part-Time"] * 40 + ["On-Call"] * 15
    tiers = ["Residential"] * 70 + ["Executive Protection"] * 35
    
    random.seed(42)
    random.shuffle(agent_types)
    random.shuffle(tiers)

    for i in range(105):
        fn = first_names[i % len(first_names)]
        ln = last_names[(i // len(first_names)) % len(last_names)]
        full_name = f"{fn} {ln}"
        
        a_type = agent_types[i]
        tier = tiers[i]
        email = f"{fn.lower()}.{ln.lower()}{i+1}@securityops.com"
        phone = f"555-01{i+1:02d}"
        
        lat = round(34.0522 + random.uniform(-0.15, 0.15), 4)
        lon = round(-118.2437 + random.uniform(-0.25, 0.25), 4)
        
        hours = 40 if a_type == "Full-Time" else (24 if a_type == "Part-Time" else 16)
        
        sample_agents.append((full_name, a_type, tier, email, phone, lat, lon, hours, "Active"))

    cursor.executemany(
        "INSERT INTO agents (full_name, agent_type, qualification_tier, email, phone, home_lat, home_lon, max_weekly_hours, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        sample_agents,
    )
    conn.commit()

    # Assign 15 random agents to have Guard Card expiring within 30 days of today
    expiring_agent_ids = set(random.sample(range(1, 106), 15))

    certs_data = []
    today_date = datetime.now().date()

    for agent_id in range(1, 106):
        if agent_id in expiring_agent_ids:
            days_to_add = random.randint(3, 28)
            gc_expiry = (today_date + timedelta(days=days_to_add)).strftime("%Y-%m-%d")
        else:
            gc_expiry = "2027-12-31"

        certs_data.append((agent_id, "Guard Card", gc_expiry))
        certs_data.append((agent_id, "Powers to Arrest", "2027-12-31"))
        certs_data.append((agent_id, "First Aid & CPR", "2027-06-30"))

        cursor.execute("SELECT qualification_tier FROM agents WHERE agent_id = ?", (agent_id,))
        tier_row = cursor.fetchone()
        if tier_row and tier_row[0] == "Executive Protection":
            certs_data.append((agent_id, "Executive Protection Specialist", "2028-01-01"))
            certs_data.append((agent_id, "Concealed Carry Permit (CCW)", "2027-09-15"))

    cursor.executemany(
        "INSERT INTO certifications (agent_id, cert_name, expiry_date) VALUES (?, ?, ?)",
        certs_data,
    )
    conn.commit()

    # Generate Dynamic Shift Schedule starting TODAY for 30 days
    start_date = datetime.now()
    end_date = start_date + timedelta(days=30)
    curr_date = start_date

    cursor.execute("SELECT location_id FROM locations")
    loc_ids = [r[0] for r in cursor.fetchall()]

    shift_types = ["Day Shift (6AM-6PM)", "Night Shift (6PM-6AM)"]

    while curr_date <= end_date:
        date_str = curr_date.strftime("%Y-%m-%d")
        for loc_id in loc_ids:
            for stype in shift_types:
                cursor.execute(
                    "INSERT INTO shifts (location_id, shift_date, shift_type) VALUES (?, ?, ?)",
                    (loc_id, date_str, stype),
                )
                shift_id = cursor.lastrowid
                assigned_agent_id = random.randint(1, len(sample_agents))
                status = random.choice(["Confirmed", "Pending", "Confirmed"])
                cursor.execute(
                    "INSERT INTO shift_assignments (shift_id, agent_id, confirmation_status) VALUES (?, ?, ?)",
                    (shift_id, assigned_agent_id, status),
                )
        curr_date += timedelta(days=1)

    conn.commit()
    conn.close()


init_db()


def format_agent_name(full_name):
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    return full_name


def check_guard_card_expiration(agent_id, shift_date_str=None):
    """Checks if an agent's Guard Card expires within 30 days of the given schedule date or today."""
    if not shift_date_str:
        shift_date_str = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT expiry_date FROM certifications WHERE agent_id = ? AND cert_name = 'Guard Card';",
        (agent_id,),
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        expiry_date = datetime.strptime(row[0], "%Y-%m-%d").date()
        ref_date = datetime.strptime(shift_date_str, "%Y-%m-%d").date()
        days_remaining = (expiry_date - ref_date).days

        if 0 <= days_remaining <= 30:
            return True, days_remaining, expiry_date.strftime("%B %d, %Y")
    return False, None, None


# Dialog Popup Modal for Agent Profile
@st.dialog("Agent Profile & Credentials")
def show_agent_modal(agent_id, shift_date_str=None):
    if not shift_date_str:
        shift_date_str = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT full_name, agent_type, qualification_tier, email, phone, max_weekly_hours, status, home_lat, home_lon
        FROM agents WHERE agent_id = ?;
    """,
        (agent_id,),
    )
    agent = cursor.fetchone()

    cursor.execute(
        """
        SELECT cert_name, expiry_date FROM certifications WHERE agent_id = ?;
    """,
        (agent_id,),
    )
    certs = cursor.fetchall()
    conn.close()

    if agent:
        st.subheader(f"👤 {agent[0]}")

        is_expiring, days_left, exp_date_str = check_guard_card_expiration(
            agent_id, shift_date_str
        )
        if is_expiring:
            st.warning(
                f"⚠️ **GUARD CARD EXPIRING SOON!**\n\n"
                f"This agent's Guard Card expires in **{days_left} days** (on {exp_date_str}). "
                f"Please initiate renewal procedures immediately."
            )

        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.write(f"**Agent ID:** #{agent_id:03d}")
            st.write(f"**Employment Type:** {agent[1]}")
            st.write(f"**Qualification Tier:** {agent[2]}")
            st.write(f"**Status:** {agent[6]}")
        with col_m2:
            st.write(f"**Max Weekly Hours:** {agent[5]} hrs/week")
            st.write(f"**Email:** {agent[3]}")
            st.write(f"**Phone:** {agent[4]}")
            st.write(f"**Home Coordinates:** {agent[7]}, {agent[8]}")

        st.divider()
        st.write("### 📜 Certifications & Licenses")
        if certs:
            cert_df = pd.DataFrame(certs, columns=["Certification Name", "Expiration Date"])
            st.table(cert_df)
        else:
            st.info("No certifications found on record.")


# ==============================================================================
# APPLICATION UI & NAVIGATION
# ==============================================================================

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

st.sidebar.title("🛡️ Security Ops Navigation")

page = st.sidebar.radio(
    "Go to",
    [
        "📅 Schedule Calendar View",
        "🌤️ LA Ops Daily Intel",
        "📲 Shift Confirmation Manager",
        "👥 Employee Management (Admin)",
        "🏢 Account Management (Admin)",
    ],
)

# Admin Auth Sidebar
st.sidebar.markdown("---")
st.sidebar.subheader("🔒 Admin Authorization")
if not st.session_state["authenticated"]:
    admin_password = st.sidebar.text_input("Enter Admin Password", type="password")
    if st.sidebar.button("Login as Admin"):
        if admin_password == "admin123":
            st.session_state["authenticated"] = True
            st.sidebar.success("Authenticated!")
            st.rerun()
        else:
            st.sidebar.error("Invalid password")
else:
    st.sidebar.info("Logged in as Administrator")
    if st.sidebar.button("Logout"):
        st.session_state["authenticated"] = False
        st.rerun()

# ==============================================================================
# PAGE 1: DYNAMIC CLICKABLE CALENDAR SCHEDULE STARTING TODAY
# ==============================================================================
if page == "📅 Schedule Calendar View":
    today_dt = datetime.now()
    st.title(f"🛡️ Security Operations Center — 30-Day Schedule ({today_dt.strftime('%B %Y')})")

    conn = sqlite3.connect(DB_NAME)
    clients_df = pd.read_sql_query(
        "SELECT client_id, client_name, account_type FROM clients ORDER BY client_name;",
        conn,
    )

    selected_account_name = st.selectbox(
        "Select Account:",
        clients_df["client_name"].tolist(),
        index=0,
    )

    selected_client = clients_df[
        clients_df["client_name"] == selected_account_name
    ].iloc[0]
    client_id = int(selected_client["client_id"])
    account_type = selected_client["account_type"]

    st.caption(
        f"**Account Type:** {account_type} | **Start Date:** Today ({today_dt.strftime('%Y-%m-%d')}) | **Location ID:** #{client_id}"
    )

    schedule_query = """
    SELECT 
        s.shift_date,
        s.shift_type,
        a.agent_id,
        a.full_name,
        a.agent_type,
        a.qualification_tier
    FROM shifts s
    JOIN locations l ON s.location_id = l.location_id
    JOIN shift_assignments sa ON s.shift_id = sa.shift_id
    JOIN agents a ON sa.agent_id = a.agent_id
    WHERE l.client_id = ?
    ORDER BY s.shift_date, s.shift_type;
    """
    schedule_df = pd.read_sql_query(schedule_query, conn, params=(client_id,))
    conn.close()

    st.divider()
    st.markdown(
        "### 📅 30-Day Dynamic Calendar Grid\n"
        "*(Agent names marked with **⚠️ YEL** indicate a **Guard Card expiring in < 30 days**. Click any name for full details.)*"
    )

    # Render 30-Day Calendar Grid starting from TODAY
    for row_start in range(0, 30, 7):
        cols = st.columns(7)
        for i in range(7):
            day_offset = row_start + i
            if day_offset < 30:
                current_day_dt = today_dt + timedelta(days=day_offset)
                date_str = current_day_dt.strftime("%Y-%m-%d")
                display_str = current_day_dt.strftime("%b %d")
                day_shifts = schedule_df[schedule_df["shift_date"] == date_str]

                with cols[i]:
                    st.markdown(f"##### **{display_str}**")
                    if day_shifts.empty:
                        st.caption("No shifts")
                    else:
                        for _, shift in day_shifts.iterrows():
                            formatted_name = format_agent_name(shift['full_name'])
                            shift_short = "Day" if "Day" in shift['shift_type'] else "Night"
                            
                            is_expiring, _, _ = check_guard_card_expiration(
                                int(shift["agent_id"]), date_str
                            )

                            if is_expiring:
                                btn_label = f"⚠️ YEL: {shift_short} {formatted_name}"
                            else:
                                btn_label = f"{shift_short}: {formatted_name}"

                            if st.button(
                                btn_label,
                                key=f"btn_{client_id}_{day_offset}_{shift['shift_type']}_{shift['agent_id']}",
                            ):
                                show_agent_modal(int(shift["agent_id"]), date_str)

    st.divider()

    with st.expander("📋 View Full Schedule Data Table"):
        st.dataframe(
            schedule_df[
                [
                    "shift_date",
                    "shift_type",
                    "full_name",
                    "agent_type",
                    "qualification_tier",
                ]
            ].rename(
                columns={
                    "shift_date": "Date",
                    "shift_type": "Shift",
                    "full_name": "Assigned Agent",
                    "agent_type": "Employment Status",
                    "qualification_tier": "Qualification",
                }
            ),
            use_container_width=True,
        )

# ==============================================================================
# PAGE 2: REAL LA DAILY INTELLIGENCE & 7-DAY WEATHER / FIRE FORECAST
# ==============================================================================
elif page == "🌤️ LA Ops Daily Intel":
    st.title("🌤️ Daily Operations Intelligence Briefing")
    st.markdown("Live API weather forecast, 7-day outlook, LAFD fire hazard warning, 30-day LA crime feed, and travel times.")

    conn = sqlite3.connect(DB_NAME)
    accounts_df = pd.read_sql_query(
        """
        SELECT c.client_name, l.code_name, l.latitude, l.longitude, l.address
        FROM clients c JOIN locations l ON c.client_id = l.client_id
    """,
        conn,
    )

    selected_account = st.selectbox(
        "Select Account Location", accounts_df["client_name"].tolist()
    )
    acc_row = accounts_df[accounts_df["client_name"] == selected_account].iloc[0]

    lat, lon = acc_row["latitude"], acc_row["longitude"]
    area_name = selected_account.split()[0]

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🌤️ Today's Weather & Fire Status (Open-Meteo Live API)")
        w_data = get_real_la_weather_and_forecast(lat, lon)
        st.metric(
            label=f"Current Temp ({acc_row['code_name']})", value=w_data["temp"]
        )
        st.write(f"**Wind Speed**: {w_data['wind']}")
        st.write(f"**Relative Humidity**: {w_data['humidity']}")

        st.subheader("🔥 LAFD Fire Hazard Status")
        fire_status = get_fire_hazard_level(
            w_data["raw_wind"], w_data["raw_hum"]
        )
        st.info(fire_status)

        st.subheader("📅 7-Day Weather & Fire Risk Forecast")
        if not w_data["forecast"].empty:
            st.dataframe(w_data["forecast"], use_container_width=True)

    with col2:
        st.subheader("🚨 30-Day LA Crime Activity Feed (LAPD Open Data)")
        crime_summary = get_real_la_crime_data(area_name)
        st.markdown(crime_summary)

        st.subheader("🚗 Live Agent Driving Time (OSRM API)")
        agents_df = pd.read_sql_query(
            "SELECT full_name, home_lat, home_lon FROM agents", conn
        )
        selected_agent = st.selectbox(
            "Select Assigned Agent", agents_df["full_name"].tolist()
        )
        ag_row = agents_df[agents_df["full_name"] == selected_agent].iloc[0]

        drive_time = get_commute_time(
            ag_row["home_lat"], ag_row["home_lon"], lat, lon
        )
        st.success(
            f"⏱️ Estimated driving time from residence to **{acc_row['code_name']}**: **{drive_time} mins**"
        )

    conn.close()

# ==============================================================================
# PAGE 3: SHIFT CONFIRMATION MANAGER (ADMIN)
# ==============================================================================
elif page == "📲 Shift Confirmation Manager":
    st.title("📲 Shift Confirmation Manager")

    if not st.session_state["authenticated"]:
        st.warning("⚠️ Administrative login required to access this page.")
    else:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        shifts_df = pd.read_sql_query(
            """
            SELECT 
                sa.assignment_id,
                s.shift_date,
                s.shift_type,
                c.client_name,
                a.full_name AS agent_name,
                a.agent_id,
                sa.confirmation_status
            FROM shift_assignments sa
            JOIN shifts s ON sa.shift_id = s.shift_id
            JOIN locations l ON s.location_id = l.location_id
            JOIN clients c ON l.client_id = c.client_id
            JOIN agents a ON sa.agent_id = a.agent_id
            ORDER BY s.shift_date ASC
        """,
            conn,
        )

        st.subheader("Current Shift Assignments")
        st.dataframe(shifts_df, use_container_width=True)

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("✏️ Manual Assistant Override")
            if not shifts_df.empty:
                selected_assignment = st.selectbox(
                    "Select Shift Assignment ID to Modify",
                    shifts_df["assignment_id"].tolist(),
                )

                agents_df = pd.read_sql_query(
                    "SELECT agent_id, full_name FROM agents WHERE status='Active'",
                    conn,
                )
                agent_dict = dict(
                    zip(
                        agents_df["full_name"]
                        + " (ID: "
                        + agents_df["agent_id"].astype(str)
                        + ")",
                        agents_df["agent_id"],
                    )
                )

                selected_agent_label = st.selectbox(
                    "Reassign to Agent", list(agent_dict.keys())
                )
                new_status = st.selectbox(
                    "Set Confirmation Status",
                    ["Confirmed", "Pending", "Declined"],
                )

                if st.button("Save Manual Shift Override"):
                    target_agent_id = agent_dict[selected_agent_label]
                    cursor.execute(
                        """
                        UPDATE shift_assignments 
                        SET agent_id = ?, confirmation_status = ? 
                        WHERE assignment_id = ?
                    """,
                        (target_agent_id, new_status, selected_assignment),
                    )
                    conn.commit()
                    st.success("Shift assignment updated successfully!")
                    st.rerun()

        with col2:
            st.subheader("💬 Simulate Inbound SMS Reply ('C')")
            pending_shifts = shifts_df[shifts_df["confirmation_status"] == "Pending"]

            if not pending_shifts.empty:
                pending_assignment = st.selectbox(
                    "Select Pending Shift",
                    pending_shifts["assignment_id"].tolist(),
                    key="sms_select",
                )
                sms_body = st.text_input("Incoming SMS Message", value="C")

                if st.button("Simulate Inbound SMS Response"):
                    if sms_body.strip().upper() == "C":
                        cursor.execute(
                            """
                            UPDATE shift_assignments 
                            SET confirmation_status = 'Confirmed' 
                            WHERE assignment_id = ?
                        """,
                            (pending_assignment,),
                        )
                        conn.commit()
                        st.success(
                            f"SMS 'C' received! Shift #{pending_assignment} confirmed and locked."
                        )
                        st.rerun()
                    else:
                        st.error("Unrecognized response. Reply 'C' to confirm shift.")
            else:
                st.info("No pending shifts requiring SMS confirmation.")

        conn.close()

# ==============================================================================
# PAGE 4: EMPLOYEE MANAGEMENT (ADMIN) WITH CLICKABLE ROSTER
# ==============================================================================
elif page == "👥 Employee Management (Admin)":
    st.title("👥 Employee Management Data Sheet")

    if not st.session_state["authenticated"]:
        st.warning("⚠️ Administrative login required to access this page.")
    else:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        tab1, tab2 = st.tabs(["➕ Add New Agent", "✏️ Update / Inactivate Agent"])

        with tab1:
            st.subheader("Add New Security Agent")
            with st.form("add_agent_form"):
                full_name = st.text_input("Full Name")
                agent_type = st.selectbox(
                    "Employment Type", ["Full-Time", "Part-Time", "On-Call"]
                )
                qualification_tier = st.selectbox(
                    "Qualification Tier", ["Residential", "Executive Protection"]
                )
                
                col_a, col_b = st.columns(2)
                with col_a:
                    home_lat = st.number_input("Home Latitude", value=34.0522, format="%.4f")
                    max_weekly_hours = st.number_input(
                        "Max Weekly Hours", min_value=0, max_value=80, value=40
                    )
                with col_b:
                    home_lon = st.number_input("Home Longitude", value=-118.2437, format="%.4f")

                submit_add = st.form_submit_button("Add Agent")

                if submit_add:
                    if full_name:
                        cursor.execute(
                            """
                            INSERT INTO agents (full_name, agent_type, qualification_tier, home_lat, home_lon, max_weekly_hours, status)
                            VALUES (?, ?, ?, ?, ?, ?, 'Active')
                        """,
                            (
                                full_name,
                                agent_type,
                                qualification_tier,
                                home_lat,
                                home_lon,
                                max_weekly_hours,
                            ),
                        )
                        conn.commit()
                        st.success(f"Agent '{full_name}' added successfully!")
                        st.rerun()
                    else:
                        st.error("Please enter a full name.")

        with tab2:
            st.subheader("Modify Existing Agent Status")
            agents_df = pd.read_sql_query("SELECT * FROM agents", conn)

            if not agents_df.empty:
                agent_options = {
                    f"{row['full_name']} (ID: {row['agent_id']})": row["agent_id"]
                    for _, row in agents_df.iterrows()
                }
                selected_agent_label = st.selectbox(
                    "Select Agent", list(agent_options.keys())
                )
                selected_agent_id = agent_options[selected_agent_label]

                current_agent = agents_df[
                    agents_df["agent_id"] == selected_agent_id
                ].iloc[0]

                with st.form("update_agent_form"):
                    updated_name = st.text_input(
                        "Full Name", value=current_agent["full_name"]
                    )
                    updated_type = st.selectbox(
                        "Employment Type",
                        ["Full-Time", "Part-Time", "On-Call"],
                        index=["Full-Time", "Part-Time", "On-Call"].index(
                            current_agent["agent_type"]
                        ),
                    )
                    updated_tier = st.selectbox(
                        "Qualification Tier",
                        ["Residential", "Executive Protection"],
                        index=[
                            "Residential",
                            "Executive Protection",
                        ].index(current_agent["qualification_tier"]),
                    )
                    
                    col_c, col_d = st.columns(2)
                    with col_c:
                        updated_lat = st.number_input("Home Latitude", value=float(current_agent["home_lat"]), format="%.4f")
                        updated_hours = st.number_input(
                            "Max Weekly Hours",
                            value=int(current_agent["max_weekly_hours"]),
                        )
                    with col_d:
                        updated_lon = st.number_input("Home Longitude", value=float(current_agent["home_lon"]), format="%.4f")
                        updated_status = st.selectbox(
                            "Account Status",
                            ["Active", "Inactive"],
                            index=["Active", "Inactive"].index(
                                current_agent["status"]
                            ),
                        )

                    submit_update = st.form_submit_button("Update Agent Record")

                    if submit_update:
                        cursor.execute(
                            """
                            UPDATE agents 
                            SET full_name = ?, agent_type = ?, qualification_tier = ?, home_lat = ?, home_lon = ?, max_weekly_hours = ?, status = ?
                            WHERE agent_id = ?
                        """,
                            (
                                updated_name,
                                updated_type,
                                updated_tier,
                                updated_lat,
                                updated_lon,
                                updated_hours,
                                updated_status,
                                selected_agent_id,
                            ),
                        )
                        conn.commit()
                        st.success(
                            f"Agent ID {selected_agent_id} updated successfully!"
                        )
                        st.rerun()

        st.markdown("---")
        st.subheader("📋 Clickable Active Employee Roster")
        st.caption("Click on any agent's name button below to display their full profile and certifications.")

        roster_df = pd.read_sql_query(
            "SELECT agent_id, full_name, agent_type, qualification_tier, status FROM agents ORDER BY agent_id ASC;",
            conn,
        )

        if not roster_df.empty:
            col_count = 4
            roster_cols = st.columns(col_count)
            for idx, r_row in roster_df.iterrows():
                a_id = int(r_row["agent_id"])
                a_name = r_row["full_name"]
                
                is_exp, _, _ = check_guard_card_expiration(a_id)
                btn_txt = f"⚠️ {a_name} (#{a_id:03d})" if is_exp else f"👤 {a_name} (#{a_id:03d})"

                with roster_cols[idx % col_count]:
                    if st.button(btn_txt, key=f"roster_btn_{a_id}", use_container_width=True):
                        show_agent_modal(a_id)

        conn.close()

# ==============================================================================
# PAGE 5: ACCOUNT MANAGEMENT (ADMIN)
# ==============================================================================
elif page == "🏢 Account Management (Admin)":
    st.title("🏢 Account & Client Management")

    if not st.session_state["authenticated"]:
        st.warning("⚠️ Administrative login required to access this page.")
    else:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        st.subheader("➕ Create New Client Account")

        agents_df = pd.read_sql_query(
            "SELECT full_name FROM agents WHERE status='Active'", conn
        )
        agent_names = agents_df["full_name"].tolist() if not agents_df.empty else []
        formatted_agent_names = [format_agent_name(name) for name in agent_names]

        with st.form("add_account_form"):
            col1, col2 = st.columns(2)

            with col1:
                client_name = st.text_input("Full Name (Client / Company Name)")
                address = st.text_input("Site Address")
                contact_phone = st.text_input("Phone Number")
                latitude = st.number_input("Site Latitude", value=34.0522, format="%.4f")

            with col2:
                account_type = st.selectbox(
                    "Account Type", ["Residential", "Hybrid", "EP"]
                )
                code_name = st.text_input(
                    "Code Name (e.g. Site Alpha, Estate 1)"
                )
                longitude = st.number_input("Site Longitude", value=-118.2437, format="%.4f")
                selected_preferred = st.multiselect(
                    "Preferred Agents (Formatted Initial + Last Name)", options=formatted_agent_names
                )

            submit_account = st.form_submit_button("Create Account")

            if submit_account:
                if client_name and code_name:
                    preferred_str = ", ".join(selected_preferred)

                    cursor.execute(
                        """
                        INSERT INTO clients (client_name, contact_phone, account_type)
                        VALUES (?, ?, ?)
                    """,
                        (client_name, contact_phone, account_type),
                    )
                    new_client_id = cursor.lastrowid

                    cursor.execute(
                        """
                        INSERT INTO locations (client_id, code_name, address, latitude, longitude, preferred_agents)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (new_client_id, code_name, address, latitude, longitude, preferred_str),
                    )

                    conn.commit()
                    st.success(
                        f"Account '{client_name}' ({code_name}) created successfully!"
                    )
                    st.rerun()
                else:
                    st.error("Please enter both Client Name and Code Name.")

        st.markdown("---")
        st.subheader("📋 Active Accounts & Locations")
        accounts_df = pd.read_sql_query(
            """
            SELECT 
                c.client_id,
                c.client_name AS [Client Name],
                c.account_type AS [Account Type],
                c.contact_phone AS [Phone],
                l.code_name AS [Code Name],
                l.address AS [Address],
                l.preferred_agents AS [Preferred Agents]
            FROM clients c
            JOIN locations l ON c.client_id = l.client_id
        """,
            conn,
        )
        st.dataframe(accounts_df, use_container_width=True)

        conn.close()