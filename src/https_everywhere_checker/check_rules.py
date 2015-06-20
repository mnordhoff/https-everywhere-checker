﻿#!/usr/bin/env python

import collections
import glob
import logging
import os
import Queue
import re
import sys
import threading
import time

from ConfigParser import SafeConfigParser

from lxml import etree

import http_client
import metrics
from rules import Ruleset
from rule_trie import RuleTrie

def convertLoglevel(levelString):
	"""Converts string 'debug', 'info', etc. into corresponding
	logging.XXX value which is returned.
	
	@raises ValueError if the level is undefined
	"""
	try:
		return getattr(logging, levelString.upper())
	except AttributeError:
		raise ValueError("No such loglevel - %s" % levelString)

def getMetricClass(metricType):
	"""Get class for metric type from config file.
	
	@raises ValueError if the metric type is unknown
	"""
	metricMap = {
		"markup": metrics.MarkupMetric,
		"bsdiff": metrics.BSDiffMetric,
	}
	
	if metricType not in metricMap:
		raise ValueError("Metric type '%s' is not known" % metricType)
	
	return metricMap[metricType]


class ComparisonTask(object):
	"""Container for objects necessary for several plain/rewritten URL comparison
		 associated with a single ruleset.
	"""
	
	def __init__(self, urls, fetcherPlain, fetcherRewriting, ruleset):
		self.urls = urls
		self.fetcherPlain = fetcherPlain
		self.fetcherRewriting = fetcherRewriting
		self.ruleset = ruleset
		self.ruleFname = ruleset.filename
	
class UrlComparisonThread(threading.Thread):
	"""Thread worker for comparing plain and rewritten URLs.
	"""
	
	def __init__(self, taskQueue, metric, thresholdDistance, autoDisable):
		"""
		Comparison thread running HTTP/HTTPS scans.
		
		@param taskQueue: Queue.Queue filled with ComparisonTask objects
		@param metric: metric.Metric instance
		@param threshold: min distance that is reported as "too big"
		"""
		self.taskQueue = taskQueue
		self.metric = metric
		self.thresholdDistance = thresholdDistance
		self.autoDisable = autoDisable
		threading.Thread.__init__(self)

	def run(self):
		while True:
			try:
				self.processTask(self.taskQueue.get())
				self.taskQueue.task_done()
			except Exception, e:
				logger.exception(e)

	def processTask(self, task):
		problems = []
		for url in task.urls:
			result = self.processUrl(url, task)
			if result:
				problems.append(result)
		if problems:
			for problem in problems:
				logging.error("%s: %s" % (task.ruleFname, problem))
			if self.autoDisable:
				disableRuleset(task.ruleset, problems)

	def processUrl(self, plainUrl, task):
		transformedUrl = task.ruleset.apply(plainUrl)
		fetcherPlain = task.fetcherPlain
		fetcherRewriting = task.fetcherRewriting
		ruleFname = task.ruleFname
		
		try:
			logging.debug("=**= Start %s => %s ****", plainUrl, transformedUrl)
			logging.debug("Fetching plain page %s", plainUrl)
			plainRcode, plainPage = fetcherPlain.fetchHtml(plainUrl)
			logging.debug("Fetching transformed page %s", transformedUrl)
			transformedRcode, transformedPage = fetcherRewriting.fetchHtml(transformedUrl)
			
			#Compare HTTP return codes - if original page returned 2xx,
			#but the transformed didn't, consider it an error in ruleset
			#(note this is not symmetric, we don't care if orig page is broken).
			#We don't handle 1xx codes for now.
			if plainRcode//100 == 2 and transformedRcode//100 != 2:
				message = "Non-2xx HTTP code: %s (%d) => %s (%d)" % (
					plainUrl, plainRcode, transformedUrl, transformedRcode)
				logging.debug(message)
				return message
			
			distance = self.metric.distanceNormed(plainPage, transformedPage)
			
			logging.debug("==== D: %0.4f; %s (%d) -> %s (%d) =====",
				distance,plainUrl, len(plainPage), transformedUrl, len(transformedPage))
			
			if distance >= self.thresholdDistance:
				logging.info("Big distance %0.4f: %s (%d) -> %s (%d). Rulefile: %s =====",
					distance, plainUrl, len(plainPage), transformedUrl, len(transformedPage), ruleFname)
		except Exception, e:
			message = "Fetch error: %s => %s: %s" % (
				plainUrl, transformedUrl, e)
			logging.debug(message)
			return message
		finally:
			logging.info("Finished comparing %s -> %s. Rulefile: %s.",
				plainUrl, transformedUrl, ruleFname)

def disableRuleset(ruleset, problems):
	logging.info("Disabling ruleset %s", ruleset.filename)
	contents = open(ruleset.filename).read()
	# Don't bother to disable rulesets that are already disabled
	if re.search("\bdefault_off=", contents):
		return
	contents = re.sub("(<ruleset [^>]*)>",
		"\\1 default_off='failed ruleset test'>", contents)

	# Since the problems are going to be inserted into an XML comment, they cannot
	# contain "--", or they will generate a parse error. Split up all "--" with a
	# space in the middle.
	safeProblems = [re.sub('--', '- -', p) for p in problems]
	# If there's not already a comment section at the beginning, add one.
	if not re.search("^<!--", contents):
		contents = "<!--\n-->\n" + contents
	problemStatement = ("""\
<!--
Disabled by https-everywhere-checker because:
%s
""" % "\n".join(problems))
	contents = re.sub("^<!--", problemStatement, contents)
	with open(ruleset.filename, "w") as f:
		f.write(contents)

def cli():
	if len(sys.argv) < 2:
		print >> sys.stderr, "check_rules.py checker.config"
		sys.exit(1)
	
	config = SafeConfigParser()
	config.read(sys.argv[1])

	filesToRead = []
	if len(sys.argv) > 2:
		filesToRead = sys.argv[2:]
	
	logfile = config.get("log", "logfile")
	loglevel = convertLoglevel(config.get("log", "loglevel"))
	if logfile == "-":
		logging.basicConfig(stream=sys.stderr, level=loglevel,
			format="%(asctime)s %(levelname)s %(message)s [%(pathname)s:%(lineno)d]")
	else:
		logging.basicConfig(filename=logfile, level=loglevel,
			format="%(asctime)s %(levelname)s %(message)s [%(pathname)s:%(lineno)d]")
		
	autoDisable = False
	if config.has_option("rulesets", "auto_disable"):
		autoDisable = config.getboolean("rulesets", "auto_disable")
	# Test rules even if they have default_off=...
	includeDefaultOff = False
	if config.has_option("rulesets", "include_default_off"):
		includeDefaultOff = config.getboolean("rulesets", "include_default_off")
	ruledir = config.get("rulesets", "rulesdir")
	checkCoverage = False
	if config.has_option("rulesets", "check_coverage"):
		checkCoverage = config.getboolean("rulesets", "check_coverage")
	certdir = config.get("certificates", "basedir")
	
	threadCount = config.getint("http", "threads")
	httpEnabled = True
	if config.has_option("http", "enabled"):
		httpEnabled = config.getboolean("http", "enabled")
	
	#get all platform dirs, make sure "default" is among them
	certdirFiles = glob.glob(os.path.join(certdir, "*"))
	havePlatforms = set([os.path.basename(fname) for fname in certdirFiles if os.path.isdir(fname)])
	logging.debug("Loaded certificate platforms: %s", ",".join(havePlatforms))
	if "default" not in havePlatforms:
		raise RuntimeError("Platform 'default' is missing from certificate directories")
	
	metricName = config.get("thresholds", "metric")
	thresholdDistance = config.getfloat("thresholds", "max_distance")
	metricClass = getMetricClass(metricName)
	metric = metricClass()
	
	# Debugging options, graphviz dump
	dumpGraphvizTrie = False
	if config.has_option("debug", "dump_graphviz_trie"):
		dumpGraphvizTrie = config.getboolean("debug", "dump_graphviz_trie")
	if dumpGraphvizTrie:
		graphvizFile = config.get("debug", "graphviz_file")
		exitAfterDump = config.getboolean("debug", "exit_after_dump")
	
	if filesToRead:
		xmlFnames = filesToRead
	else:
		xmlFnames = glob.glob(os.path.join(ruledir, "*.xml"))
	trie = RuleTrie()
	
	rulesets = []
	coverageProblemsExist = False
	for xmlFname in xmlFnames:
		logging.debug("Parsing %s", xmlFname)
		try:
			ruleset = Ruleset(etree.parse(file(xmlFname)).getroot(), xmlFname)
		except:
			logger.error("Exception parsing %s: %s" % (xmlFname, e))
		if ruleset.defaultOff and not includeDefaultOff:
			logging.debug("Skipping rule '%s', reason: %s", ruleset.name, ruleset.defaultOff)
			continue
		# Check whether ruleset coverage by tests was sufficient.
		if checkCoverage:
			problems = ruleset.getCoverageProblems()
			for problem in problems:
				coverageProblemsExist = True
				logging.error(problem)
		trie.addRuleset(ruleset)
		rulesets.append(ruleset)
	
	# Trie is built now, dump it if it's set in config
	if dumpGraphvizTrie:
		logging.debug("Dumping graphviz ruleset trie")
		graph = trie.generateGraphizGraph()
		if graphvizFile == "-":
			graph.dot()
		else:
			with file(graphvizFile, "w") as gvFd:
				graph.dot(gvFd)
		if exitAfterDump:
			sys.exit(0)
	
	fetchOptions = http_client.FetchOptions(config)
	fetcherMap = dict() #maps platform to fetcher
	
	platforms = http_client.CertificatePlatforms(os.path.join(certdir, "default"))
	for platform in havePlatforms:
		#adding "default" again won't break things
		platforms.addPlatform(platform, os.path.join(certdir, platform))
		fetcher = http_client.HTTPFetcher(platform, platforms, fetchOptions, trie)
		fetcherMap[platform] = fetcher
	
	#fetches pages with unrewritten URLs
	fetcherPlain = http_client.HTTPFetcher("default", platforms, fetchOptions)
	
	urlList = []
	if config.has_option("http", "url_list"):
		with file(config.get("http", "url_list")) as urlFile:
			urlList = [line.rstrip() for line in urlFile.readlines()]
			
	if httpEnabled:
		taskQueue = Queue.Queue(1000)
		startTime = time.time()
		testedUrlPairCount = 0
		config.getboolean("debug", "exit_after_dump")

		for i in range(threadCount):
			t = UrlComparisonThread(taskQueue, metric, thresholdDistance, autoDisable)
			t.setDaemon(True)
			t.start()

		# set of main pages to test
		mainPages = set(urlList)
		# If list of URLs to test/scan was not defined, use the test URL extraction
		# methods built into the Ruleset implementation.
		if not urlList:
			for ruleset in rulesets:
				testUrls = []
				for test in ruleset.tests:
					if not ruleset.excludes(test.url):
						testedUrlPairCount += 1
						testUrls.append(test.url)
					else:
						logging.debug("Skipping landing page %s", test.url)
				task = ComparisonTask(testUrls, fetcherPlain, fetcher, ruleset)
				taskQueue.put(task)
		taskQueue.join()
		logging.info("Finished in %.2f seconds. Loaded rulesets: %d, URL pairs: %d.",
			time.time() - startTime, len(xmlFnames), testedUrlPairCount)

	if checkCoverage:
		if coverageProblemsExist:
			return 1 # exit with error code
		else:
			return 0 # exit with success

if __name__ == '__main__':
	sys.exit(cli())
