MODULE_TOPDIR = ../..

PGM = i.ai

include $(MODULE_TOPDIR)/include/Make/Script.make

default: script
	if [ ! -d $(HTMLDIR) ]; then \
		$(MKDIR) $(HTMLDIR); \
	fi
	if [ ! -f $(HTMLDIR)/$(PGM).html ] ; then \
		$(INSTALL) -m 644 $(PGM).html $(HTMLDIR)/$(HTMLDIR)/$(PGM).html ; \
	fi
