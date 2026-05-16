import time
import json
import logging
import requests
from bs4 import BeautifulSoup
from typing import Dict, Any, Optional
import concurrent.futures

# Configure basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

class BaseScraper:
    """
    Base class for maritime registry scrapers.
    Handles rate limiting, basic error catching, and standardizes output.
    """
    def __init__(self, delay_seconds: float = 2.0):
        self.delay_seconds = delay_seconds
        self.session = requests.Session()
        # Spoof a standard browser user-agent to bypass basic checks
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetches HTML content with rate limiting and error handling."""
        time.sleep(self.delay_seconds)
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            # Anti-bot check: if we get a 200 but the page is a Cloudflare challenge
            if "Cloudflare" in response.text or "cf-browser-verification" in response.text:
                logging.warning(f"Cloudflare bot protection detected on {url}.")
                raise PermissionError("Blocked by anti-bot challenge.")
                
            return response.text
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error fetching {url}: {e}")
            raise
        except PermissionError as e:
            logging.error(f"Access error fetching {url}: {e}")
            raise

    def scrape_vessel(self, imo_number: str) -> Dict[str, Any]:
        """Interface method to be implemented by child classes."""
        raise NotImplementedError("Subclasses must implement this method.")


class MarineTrafficScraper(BaseScraper):
    """Scraper tailored for MarineTraffic (notoriously heavily protected)."""
    
    BASE_URL = "https://www.marinetraffic.com/en/ais/details/ships/imo:"

    def scrape_vessel(self, imo_number: str) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{imo_number}"
        logging.info(f"Attempting to scrape MarineTraffic for IMO: {imo_number}")
        
        try:
            html_content = self._fetch_page(url)
            # -------------------------------------------------------------
            # SCRAPING LOGIC (Scaffolding)
            # If the request succeeds (e.g. proxy used), parse the HTML:
            # -------------------------------------------------------------
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Example parsing logic (selectors frequently change on MT)
            # In a real environment, you'd find the specific <div> or <meta> tags.
            vessel_name = soup.find("h1", class_="font-200 no-margin").text.strip() if soup.find("h1", class_="font-200 no-margin") else "UNKNOWN"
            
            return {
                "source": "MarineTraffic",
                "imo": imo_number,
                "name": vessel_name,
                "mmsi": "EXTRACTED_MMSI", # Scaffolding
                "flag": "EXTRACTED_FLAG"  # Scaffolding
            }
            
        except (requests.exceptions.RequestException, PermissionError):
            logging.warning("MarineTraffic live scrape failed. Falling back to mock data.")
            return self._fallback_mock(imo_number)

    def _fallback_mock(self, imo_number: str) -> Dict[str, Any]:
        """Returns mock JSON data when the live site blocks the scraper."""
        mocks = {
            "9988776": {"name": "SEA SHADOW", "mmsi": "422000000", "flag": "IR", "owner": "National Iranian Tanker Co"},
            "9123456": {"name": "OCEAN VOYAGER", "mmsi": "366000000", "flag": "US", "owner": "Global Shipping Logistics"}
        }
        data = mocks.get(imo_number, {"name": "UNKNOWN_VESSEL", "mmsi": "UNKNOWN", "flag": "UNKNOWN", "owner": "UNKNOWN"})
        
        return {
            "source": "MarineTraffic (MOCK)",
            "imo": imo_number,
            "status": "success",
            "data": data
        }


class EquasisScraper(BaseScraper):
    """Scraper tailored for Equasis (Requires Login & CAPTCHA)."""
    
    BASE_URL = "https://www.equasis.org/EquasisWeb/restricted/ShipList?fs=ShipSearch&imo="

    def scrape_vessel(self, imo_number: str) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{imo_number}"
        logging.info(f"Attempting to scrape Equasis for IMO: {imo_number}")
        
        try:
            # Note: Equasis strictly requires an authenticated session cookie.
            # Without proper authentication handled prior to this request, it will 302 redirect.
            html_content = self._fetch_page(url)
            
            # -------------------------------------------------------------
            # SCRAPING LOGIC (Scaffolding)
            # -------------------------------------------------------------
            soup = BeautifulSoup(html_content, 'html.parser')
            # Extract data from Equasis tables...
            
            return {
                "source": "Equasis",
                "imo": imo_number,
                "data": "EXTRACTED_DATA"
            }
            
        except (requests.exceptions.RequestException, PermissionError):
            logging.warning("Equasis live scrape failed (Auth/Captcha). Falling back to mock data.")
            return self._fallback_mock(imo_number)

    def _fallback_mock(self, imo_number: str) -> Dict[str, Any]:
        """Returns mock JSON data containing Equasis-specific inspection/ownership data."""
        mocks = {
            "9988776": {
                "registered_owner": "SHADOW FLEET CORP", 
                "company_imo": "5551234",
                "psc_inspections": "High Risk - Detained 2023"
            },
            "9123456": {
                "registered_owner": "Global Shipping Logistics", 
                "company_imo": "1119999",
                "psc_inspections": "Standard Risk - No Detentions"
            }
        }
        data = mocks.get(imo_number, {"registered_owner": "UNKNOWN", "company_imo": "UNKNOWN", "psc_inspections": "UNKNOWN"})
        
        return {
            "source": "Equasis (MOCK)",
            "imo": imo_number,
            "status": "success",
            "data": data
        }


class RegistryCrossReferencer:
    """
    Orchestrates parallel data retrieval across multiple maritime registries.
    
    Utilizes a ThreadPoolExecutor to concurrently scrape Equasis and MarineTraffic,
    merging the results into a unified context dictionary for the LangGraph agents.
    This significantly reduces the latency of the Data Retrieval node.
    """
    def __init__(self):
        self.scrapers = {
            "marine_traffic": MarineTrafficScraper(),
            "equasis": EquasisScraper()
        }

    def _run_scraper(self, name: str, scraper: BaseScraper, imo: str) -> tuple:
        try:
            return name, scraper.scrape_vessel(imo)
        except Exception as e:
            logging.error(f"Scraper {name} failed for IMO {imo}: {e}")
            return name, {"error": str(e), "source": name}

    def scrape_parallel(self, imo_number: str) -> Dict[str, Any]:
        results = {}
        logging.info(f"Starting parallel scrape for IMO: {imo_number}")
        
        # Run scrapers concurrently using a thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.scrapers)) as executor:
            future_to_scraper = {
                executor.submit(self._run_scraper, name, scraper, imo_number): name
                for name, scraper in self.scrapers.items()
            }
            
            for future in concurrent.futures.as_completed(future_to_scraper):
                name, data = future.result()
                results[name] = data
                
        # Merge logic
        merged = {
            "imo": imo_number,
            "scraped_at": time.time(),
            "sources": [],
            "vessel_name": "UNKNOWN",
            "mmsi": "UNKNOWN",
            "flag": "UNKNOWN",
            "registered_owner": "UNKNOWN",
            "company_imo": "UNKNOWN",
            "psc_inspections": "UNKNOWN",
            "raw_results": results
        }
        
        # Extract from MarineTraffic
        if "marine_traffic" in results and "error" not in results["marine_traffic"]:
            mt_data = results["marine_traffic"].get("data", results["marine_traffic"])
            merged["vessel_name"] = mt_data.get("name", merged["vessel_name"])
            merged["mmsi"] = mt_data.get("mmsi", merged["mmsi"])
            merged["flag"] = mt_data.get("flag", merged["flag"])
            merged["sources"].append(results["marine_traffic"].get("source", "MarineTraffic"))
            
        # Extract from Equasis
        if "equasis" in results and "error" not in results["equasis"]:
            eq_data = results["equasis"].get("data", results["equasis"])
            if isinstance(eq_data, dict):
                merged["registered_owner"] = eq_data.get("registered_owner", merged["registered_owner"])
                merged["company_imo"] = eq_data.get("company_imo", merged["company_imo"])
                merged["psc_inspections"] = eq_data.get("psc_inspections", merged["psc_inspections"])
            merged["sources"].append(results["equasis"].get("source", "Equasis"))

        return merged


if __name__ == "__main__":
    # Test execution
    print("\n--- Testing Registry Cross Referencer ---")
    cross_referencer = RegistryCrossReferencer()
    result = cross_referencer.scrape_parallel("9988776")
    print(json.dumps(result, indent=2))
