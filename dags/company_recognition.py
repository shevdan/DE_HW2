from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.operators.postgres_operator import PostgresOperator
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python import BranchPythonOperator
from datetime import datetime, timedelta
from airflow.utils.task_group import TaskGroup

from airflow.operators.python import PythonOperator
from airflow.operators.postgres_operator import PostgresOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.models import Variable

from easyocr import Reader

from peopledatalabs import PDLPY
import json


import requests
from bs4 import BeautifulSoup

import logging

# from openai import OpenAI

# from google.oauth2 import service_account


def get_company_info():
    urls = ti.xcom_pull("ocr_images")
    client = PDLPY(api_key=Variable.get("PEOPLEDATALABS_API_KEY"))

    pg_hook = PostgresHook(postgres_conn_id='custom_postgres')

    for url in urls:
        params = {"website": url}
        try:
            json_response = client.company.enrichment(**params).json()
            name = json_response.get("name")
            display_name = json_response.get("display_name")
            size = json_response.get("size")
            founded = json_response.get("founded")

            if name and display_name and size and founded:

                pg_hook.run(
                    "INSERT INTO companies (company_url, company_name, size, display_name, founded) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (company_url) DO NOTHING",
                    parameters=(url, name, size, display_name, founded)
                )
        except Exception as e:
            logging.info(f"Error processing URL {url}: {e}")
            continue



def scrape_images():
    url = Variable.get("URL")
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')
    images = set(img['src'] for img in soup.find_all('img') if 'src' in img.attrs)

    pg_hook = PostgresHook(postgres_conn_id='custom_postgres')
    
    existing_images = {row[0] for row in pg_hook.get_records("SELECT image_url FROM image_links")}

    new_images = images - existing_images


    logging.info(images)

    for image in new_images:
        pg_hook.run("INSERT INTO image_links (image_url, processed) VALUES (%s, false)", parameters=(image,))


def process_images():
    pg_hook = PostgresHook(postgres_conn_id='custom_postgres')
    result = pg_hook.get_records("SELECT image_url FROM image_links WHERE processed = false")
    urls = []

    for row in result:
        image_url = row[0]
        reader = Reader(['en'])
        try:
            ocr_results = reader.readtext(image_url)
            text = ' '.join([res[1] for res in ocr_results])
            words = text.split()

            for i in range(1, len(words)):
                if words[i] in ['com', 'ua', 'gov', 'org', 'net']: 
                    possible_url = words[i-1] + '.' + words[i]
                    urls.append(possible_url)

        except Exception as e:
            logging.info(f"Error processing image {image_url}: {e}")
            continue
        finally:
            pg_hook.run("UPDATE image_links SET processed = true WHERE image_url = %s", parameters=(image_url,))
        
    return urls


with DAG('image_processing_dag', start_date=datetime(2023, 12, 9), schedule_interval="@daily", catchup=True) as dag:
    create_image_info_table = PostgresOperator(
        task_id='create_image_info_table',
        postgres_conn_id='custom_postgres',
        sql="""
            CREATE TABLE IF NOT EXISTS image_links (
            id SERIAL PRIMARY KEY,
            image_url TEXT NOT NULL,
            processed BOOLEAN NOT NULL DEFAULT false
        );
        """
    )

    create_company_info_table = PostgresOperator(
        task_id='create_company_info_table',
        postgres_conn_id='custom_postgres',
        sql="""
            CREATE TABLE IF NOT EXISTS companies (
            company_url VARCHAR(255) PRIMARY KEY
            company_name VARCHAR(255) NOT NULL
            size VARCHAR(255)
            display_name VARCHAR(255),
            founded INT
        );
        """
    )

    scrape_task = PythonOperator(
        task_id='scrape_images',
        python_callable=scrape_images,
        )
    
    ocr_process = PythonOperator(
        task_id='ocr_images',
        python_callable=process_images,
        )
    
    company_info = PythonOperator(
        task_id = "company_info",
        python_callable=get_company_info,
    )


    create_image_info_table >> create_company_info_table >> scrape_task >> ocr_process >> company_info



