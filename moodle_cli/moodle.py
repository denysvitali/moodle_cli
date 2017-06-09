import bs4
import json
import logging
import os
import os.path
import pickle
import re
import requests
import time
import html

from getpass import getpass
from urllib.parse import urlparse, urljoin

from moodle_cli.constants import *
from moodle_cli.extractors.extractors import Extractors
from moodle_cli.extractors.url import URLCollector
from moodle_cli.utils import DownloadProgess

from moodle_cli import logger


class MoodleCourseDownloader:

	def __init__(self, save_dir, base_url, keep_old_files=True):
		self._keep_old_files = keep_old_files
		self.base_url = base_url
		self.base_dir = save_dir
		self._session = requests.Session()
		self.load_cookies()


	def load_cookies(self):
		cookies = None
		self.cookie_path = os.path.join(os.getcwd(), 'cookie.pickle')
		if os.path.exists(self.cookie_path):
			with open(self.cookie_path, 'rb') as fd:
				cookies = pickle.load(fd)

		if cookies:
			self._session.cookies.update(cookies)

	def shutdown(self):
		cookies = self._session.cookies

		with open(self.cookie_path, 'wb') as fd:
			pickle.dump(cookies, fd, protocol=pickle.HIGHEST_PROTOCOL)

		self._session.close()

	def extract_course_pages(self):
		r = self._session.get(urljoin(self.base_url, '/my'))

		print(r.text)

		if r.status_code != 200:
			raise ConnectionError('Failed to obtain your moodle course pages')
		r = self._handle_login(r)

		print('Loading moodle courses ...', end=' ')

		soup = bs4.BeautifulSoup(r.text, 'html.parser')
		courses = [ (course_a['title'], course_a['href']) for course_a in soup.select('.course_list div h2.title a')]

		print('Done', end='\n\n')
		return courses

	def extract_and_download(self, courses):

		for name, url in courses:

			logger.info('Loading course page  %s ...', name)
			retrys = 0
			while retrys < 3:
				try:
					r = self._session.get(url)
					if r.status_code != 200:
						raise ConnectionError('Load of course \"{0}\" failded: statuse_code = {1}'.format(name, r.status_code))
					break
				except:
					logger.exception('Failed to connect to %s', url)
					retrys += 1

			r = self._handle_login(r)

			soup = bs4.BeautifulSoup(r.text, 'html.parser')
			sections =  [self._build_section_dict(section) for section in soup.select('ul.topics li.section')]

			if len(sections) == 0:
				sections = [self._build_section_dict(section) for section in soup.select('ul.weeks li.section')]

			logger.info('Done')

			if logger.isEnabledFor(logging.DEBUG):
				logger.debug("Extracted sections: \"%s\"", sections)

			self.download_sections(os.path.join(self.base_dir, name), sections)

		logger.info('Done')

	def _build_section_dict(self, section):
		section_dict = {
			'resources': [],
			'links': URLCollector(),
			'pages': []
		}

		section_dict['title'] = section.get('aria-label')
		logger.debug("Found section \"%s\"", section_dict['title'])
		for resource in section.select('ul.section li.activity'):
			type = self._determine_type(resource)
			extractor = Extractors.get_by_type(self, type)

			if extractor is not None:
				activity = extractor.extract(resource)

				if activity:
					logger.debug("Extracted %s \"%s\" - %s", type, activity['title'], activity['url'])

					if type == ACTIVITY_TYPE_URL:
						section_dict['links'].append(activity)

					if type == ACTIVITY_TYPE_RESOURCE:
						section_dict['resources'].append(activity)

					if type == ACTIVITY_TYPE_PAGE:
						section_dict['pages'].append(activity)

					if type == ACTIVITY_TYPE_FOLDER:
						for res in activity['files']:
							section_dict['resources'].append(res)



		return section_dict

	def _determine_type(self, resource):
		"""
		Determines the type of this section by checking for
		existing of a specific class
		"""
		cls = resource.get('class')
		for _type, _type_cls in TYPE_CLS_MAP.items():
			if _type_cls in cls:
				return _type
		# Non supported type detected
		return None


	def download_sections(self, basedir, sections):

		default_mode = 0o755
		basedir = os.path.abspath(basedir)

		# Ensure basedir exists
		if not os.path.isdir(basedir):
			os.makedirs(basedir, mode=default_mode, exist_ok=True)

		for i, section in enumerate(sections):

			if section['title']:
				title = re.sub(r'[\s\\/]+', '_', section['title']).strip()
				logger.info("\nLoading Section %s\n======================%s\n", title, "=" * len(title))
				title = "{0:02}_{1}".format(i, title)

				section_dir = os.path.join(basedir, title)

				if not os.path.isdir(section_dir):
					os.mkdir(section_dir, mode=default_mode)

				for resource in section['resources']:
					url = resource['url']

					if 'filename' in resource:
						file_name = resource['filename']
					else:
						file_name = os.path.basename(urlparse(url).path)

					file_path = os.path.join(section_dir, file_name)

					try:
						if os.path.exists(file_path):
							if self._keep_old_files:
								continue
							os.remove(file_path)
						self.download_file(url, file_path, file_name)
					except Exception as e:
						logger.error("Failed to download %s: %s", url, e)

				if section['links'].content:
					try:
						logger.info('Downloaded links.md')
						link_file_path = os.path.join(section_dir, section['links'].name)
						with open(link_file_path, 'w') as fd:
							fd.write(section['links'].content)
					except:
						logger.error('Failed to write links.md')

				for page in section['pages']:
					try:
						page_name = "{0}.md".format(page['title'])
						logger.info('Downloaded %s', page_name)
						page_file_path = os.path.join(section_dir, )
						with open(page_file_path, 'w') as fd:
							fd.write(page['content'])
					except:
						logger.error('Failed to load page  %s', page['title'])

				if len(os.listdir(section_dir)) == 0:
					os.rmdir(section_dir)



	def download_file(self, url, file_path, file_name):

		bytes_loaded = 0
		start_time = time.time()
		max_epochs = 3
		epoch_count = 0

		r = self._session.get(url, stream=True)

		try:
			file_size = int(r.headers.get('Content-Length', -1))
		except ValueError as e:
			logger.error('Failed to obtain file size: no valid Content-Length received')
			raise e

		progress = DownloadProgess(file_name, file_size)
		with open(file_path, 'wb') as fd:
			for chunk in r.iter_content(chunk_size=1024):
				if not chunk:
					continue
				fd.write(chunk)
				progress.update(len(chunk))
			progress.end()
		logger.debug('Finish downloading: %s', url)

	def _is_login_url(self, r):
			return r.url.endswith('login/index.php')

	def _handle_login(self, r):
		"""
		Handles and perfoms a login for given response object
		if necessary.

		r - A response object for which a login is to be treated.
		"""


		if self._is_login_url(r):
			login_data = self._request_login_data()
			if login_data is None:
				raise RuntimeError('Course-Download isn\'t possible login not performed')

			#r = self._session.post(urljoin(self.base_url, 'login/index.php'), data=login_data)
			fdata = {
				'user_idp': 'https://login2.supsi.ch/idp/shibboleth',
				'Select': 'Select'
			}

			r = self._session.get('https://www2.icorsi.ch/auth/shibboleth/')
			#print(r.url) # wayf.switch.ch ...

			r = self._session.post(r.url, fdata)

			r = self._session.post(r.url, login_data)

			#print(r.url) # login2.supsi.ch ...

			#r = self._session.get(r.url) # SAML request

			samlResp = re.search('name="SAMLResponse" value="(.*?)"', r.text)
			relayState = re.search('name="RelayState" value="(.*?)"', r.text)
			action = re.search('form action="(.*?)"', r.text)

			print(r.text)

			samlResp = html.unescape(samlResp.group(1))
			relayState = html.unescape(relayState.group(1))
			action = html.unescape(action.group(1))

			r = self._session.post(action, {
				'RelayState': relayState,
				'SAMLResponse': samlResp
			})

			if self._is_login_url(r):
				raise RuntimeError('Login failed credentials not valid')

		return r

	def _request_login_data(self):
		print('Login is required, please enter your moodle credentials!')
		return {
			'j_username': input('username: '),
			'j_password': getpass('password: '),
			'_eventId_proceed': ''
		}
