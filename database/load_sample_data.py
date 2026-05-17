"""
Maritime Intelligence Knowledge Graph - Dummy Data Loader
This script connects to the Neo4j instance and populates it with a sample
dataset representing vessels, companies, ports, sanctions, voyages, and events.
It demonstrates the graph schema in a realistic scenario.
"""
from neo4j import GraphDatabase
import uuid
from datetime import date, datetime

URI = "neo4j://localhost:7687"
AUTH = ("neo4j", "maritime123")

def load_data(driver):
    with driver.session() as session:
        # 1. Create Flags
        session.run("""
            MERGE (f1:Flag {country_code: 'LR'}) SET f1.name = 'Liberia'
            MERGE (f2:Flag {country_code: 'PA'}) SET f2.name = 'Panama'
            MERGE (f3:Flag {country_code: 'IR'}) SET f3.name = 'Iran'
        """)

        # 2. Create Companies
        session.run("""
            MERGE (c1:Company {company_imo: '5123456'})
            SET c1.name = 'Global Maritime Holdings', c1.country_of_registration = 'Liberia'
            MERGE (c2:Company {company_imo: '5987654'})
            SET c2.name = 'Tehran Shipping Lines', c2.country_of_registration = 'Iran'
            MERGE (c3:Company {company_imo: '5444333'})
            SET c3.name = 'Frontline Managers', c3.country_of_registration = 'Norway'
        """)

        # 3. Create Sanctions
        session.run("""
            MERGE (s1:Sanction {sanction_id: 'OFAC-IRAN-01'})
            SET s1.program = 'IRAN', s1.authority = 'OFAC', s1.issue_date = date('2018-11-05')
        """)

        # Sanction the Iranian company
        session.run("""
            MATCH (c:Company {company_imo: '5987654'})
            MATCH (s:Sanction {sanction_id: 'OFAC-IRAN-01'})
            MERGE (c)-[:SANCTIONED_BY]->(s)
        """)

        # 4. Create Vessels & Ownership
        session.run("""
            MERGE (v1:Vessel {imo: '9123456'})
            SET v1.name = 'OCEAN VOYAGER', v1.vessel_type = 'Crude Oil Tanker', v1.mmsi = '636012345', v1.built_year = 2010

            WITH v1
            MATCH (f:Flag {country_code: 'LR'})
            MATCH (owner:Company {company_imo: '5123456'})
            MATCH (mgr:Company {company_imo: '5444333'})
            
            MERGE (v1)-[r1:REGISTERED_UNDER]->(f) SET r1.start_date = date('2015-01-01')
            MERGE (v1)-[r2:OWNED_BY]->(owner) SET r2.role = 'Registered Owner', r2.start_date = date('2015-01-01')
            MERGE (v1)-[r3:MANAGED_BY]->(mgr) SET r3.role = 'Commercial Manager', r3.start_date = date('2018-05-10')
        """)

        session.run("""
            MERGE (v2:Vessel {imo: '9988776'})
            SET v2.name = 'SEA SHADOW', v2.vessel_type = 'Crude Oil Tanker', v2.mmsi = '422000111', v2.built_year = 2005

            WITH v2
            MATCH (f:Flag {country_code: 'IR'})
            MATCH (owner:Company {company_imo: '5987654'})
            MATCH (s:Sanction {sanction_id: 'OFAC-IRAN-01'})
            
            MERGE (v2)-[r1:REGISTERED_UNDER]->(f) SET r1.start_date = date('2005-01-01')
            MERGE (v2)-[r2:OWNED_BY]->(owner) SET r2.role = 'Registered Owner'
            MERGE (v2)-[:SANCTIONED_BY]->(s)
        """)

        # 5. Create Ports
        session.run("""
            MERGE (p1:Port {unlocode: 'USNYC'})
            SET p1.name = 'New York', p1.country = 'USA', p1.location = point({latitude: 40.7128, longitude: -74.0060})
            
            MERGE (p2:Port {unlocode: 'NLRTM'})
            SET p2.name = 'Rotterdam', p2.country = 'Netherlands', p2.location = point({latitude: 51.9225, longitude: 4.47917})

            MERGE (p3:Port {unlocode: 'IRBND'})
            SET p3.name = 'Bandar Abbas', p3.country = 'Iran', p3.location = point({latitude: 27.1833, longitude: 56.2667})
        """)

        # 6. Create Voyages and Events
        # Ocean Voyager Voyage
        session.run("""
            MATCH (v1:Vessel {imo: '9123456'})
            MATCH (p_start:Port {unlocode: 'USNYC'})
            MATCH (p_end:Port {unlocode: 'NLRTM'})
            
            MERGE (voy1:Voyage {voyage_id: 'VOY-9123456-001'})
            SET voy1.status = 'Completed'
            
            MERGE (v1)-[:UNDERTOOK]->(voy1)
            MERGE (voy1)-[:DEPARTED_FROM {timestamp: datetime('2023-10-01T08:00:00Z'), draft_out: 10.5}]->(p_start)
            MERGE (voy1)-[:ARRIVED_AT {timestamp: datetime('2023-10-15T14:30:00Z'), draft_in: 10.2}]->(p_end)
        """)

        # Sea Shadow Event (AIS Gap followed by Port Visit)
        session.run("""
            MATCH (v2:Vessel {imo: '9988776'})
            MATCH (p_end:Port {unlocode: 'IRBND'})
            
            // Create the AIS Gap event
            MERGE (e:Event {event_id: 'EVT-AIS-001'})
            SET e.event_type = 'AIS_GAP', 
                e.start_time = datetime('2023-11-01T12:00:00Z'),
                e.end_time = datetime('2023-11-05T12:00:00Z'),
                e.description = 'Extended AIS outage in Middle East Gulf'
                
            MERGE (v2)-[:INVOLVED_IN]->(e)
            
            // Create the voyage that ends at Bandar Abbas right after the gap
            MERGE (voy2:Voyage {voyage_id: 'VOY-9988776-001'})
            SET voy2.status = 'Completed'
            
            MERGE (v2)-[:UNDERTOOK]->(voy2)
            MERGE (voy2)-[:ARRIVED_AT {timestamp: datetime('2023-11-06T10:00:00Z'), draft_in: 14.5}]->(p_end)
        """)

        print("Sample data loaded successfully.")

if __name__ == "__main__":
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        # Verify connection
        driver.verify_connectivity()
        load_data(driver)
