MODULE_TOPDIR = ../..

PGM = i.ai

ETCFILES = iai_agents iai_catalog iai_context iai_llm iai_rag iai_registry \
	iai_report iai_shell iai_tools iai_validate

include $(MODULE_TOPDIR)/include/Make/Script.make
include $(MODULE_TOPDIR)/include/Make/Python.make

default: script
