##############################################################################
#
#   MRC FGU CGAT
#
#   $Id$
#
#   Copyright (C) 2009 Andreas Heger
#
#   This program is free software; you can redistribute it and/or
#   modify it under the terms of the GNU General Public License
#   as published by the Free Software Foundation; either version 2
#   of the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
###############################################################################
"""===========================
Pipeline template
===========================

:Author: Andreas Heger
:Release: $Id$
:Date: |today|
:Tags: Python

.. Replace the documentation below with your own description of the
   pipeline's purpose

Overview
========

This pipeline computes the word frequencies in the configuration
files :file:``pipeline.ini` and :file:`conf.py`.

Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning` on general
information how to use CGAT pipelines.

Configuration
-------------

The pipeline requires a configured :file:`pipeline.ini` file.
CGATReport report requires a :file:`conf.py` and optionally a
:file:`cgatreport.ini` file (see :ref:`PipelineReporting`).

Default configuration files can be generated by executing:

   python <srcdir>/pipeline_macs2.py config

Input files
-----------
- Bam files & bai indexes

- sample files should be of format: <exp>-<cond>-<treatment>-<sample>.genome.bam
- with matching file for input:     <exp>-<cond>-<treatment>-<WCE>.genome.bam
- if only 1 input is needed for all samples it can be specified in pipeline.ini

Requirements
------------

The pipeline requires the results from
:doc:`pipeline_annotations`. Set the configuration variable
:py:data:`annotations_database` and :py:data:`annotations_dir`.

On top of the default CGAT setup, the pipeline requires the following
software to be in the path:

.. Add any additional external requirements such as 3rd party software
   or R modules below:

Requirements:

* samtools >= 1.1

Pipeline output
===============

.. Describe output files of the pipeline here

Glossary
========

.. glossary::


Code
====

"""
from ruffus import *

import sys
import os
import sqlite3
import CGAT.Experiment as E
import CGATPipelines.Pipeline as P
import CGAT.BamTools as BamTools

# load options from the config file
PARAMS = P.getParameters(
    ["%s/pipeline.ini" % os.path.splitext(__file__)[0],
     "../pipeline.ini",
     "pipeline.ini"])

# add configuration values from associated pipelines
#
# 1. pipeline_annotations: any parameters will be added with the
#    prefix "annotations_". The interface will be updated with
#    "annotations_dir" to point to the absolute path names.
PARAMS.update(P.peekParameters(
    PARAMS["annotations_dir"],
    "pipeline_annotations.py",
    on_error_raise=__name__ == "__main__",
    prefix="annotations_",
    update_interface=True))


# if necessary, update the PARAMS dictionary in any modules file.
# e.g.:
#
# import CGATPipelines.PipelineGeneset as PipelineGeneset
# PipelineGeneset.PARAMS = PARAMS
#
# Note that this is a hack and deprecated, better pass all
# parameters that are needed by a function explicitely.

# -----------------------------------------------
# Utility functions
def connect():
    '''utility function to connect to database.

    Use this method to connect to the pipeline database.
    Additional databases can be attached here as well.

    Returns an sqlite3 database handle.
    '''

    dbh = sqlite3.connect(PARAMS["database"])
    statement = '''ATTACH DATABASE '%s' as annotations''' % (
        PARAMS["annotations_database"])
    cc = dbh.cursor()
    cc.execute(statement)
    cc.close()

    return dbh


# ---------------------------------------------------
# Specific pipeline tasks

#####################################################
####                Mapping                      ####
#####################################################
@follows(connect, mkdir("bowtie2.dir"))
@transform("data.dir/*.fastq.1.gz",
           regex(r"data.dir/(.*).fastq.1.gz"),
           r"bowtie2.dir/\1.genome.bam")
def mapBowtie2_PE(infile, outfile):
    '''Map reads with Bowtie2'''

    if len(infile) == 0:
        pass

    read1 = infile
    read2 = infile.replace(".1.gz", ".2.gz")
    
    job_memory = "2G"
    job_threads = "12"
    
    log = outfile + "_bowtie2.log"
    tmp_dir = "$SCRATCH_DIR"

    options = PARAMS["bowtie2_options"]
    genome = os.path.join(PARAMS["bowtie2_genomedir"], PARAMS["bowtie2_genome"])

    statement = '''tmp=`mktemp -p %(tmp_dir)s`; checkpoint;
                   bowtie2 
                     --quiet 
                     --threads 12 
                     -x %(genome)s
                     -1 %(read1)s 
                     -2 %(read2)s
                     %(options)s
                     1> $tmp 
                     2> %(log)s; checkpoint;
                   samtools sort -O BAM -o %(outfile)s $tmp; checkpoint;
                   rm $tmp''' % locals()

    print statement

    P.run()

    
@transform("data.dir/*.fastq.gz",
           regex(r"data.dir/(.*).fastq.gz"),
           r"bowtie2.dir/\1.genome.bam")
def mapBowtie2_SE(infile, outfile):
    '''Map reads with Bowtie2'''

    if len(infile) == 0:
        pass

    job_memory = "2G"
    job_threads = "12"
    
    log = outfile + "_bowtie2.log"
    tmp_dir = "$SCRATCH_DIR"

    options = PARAMS["bowtie2_options"]
    genome = os.path.join(PARAMS["bowtie2_genomedir"], PARAMS["bowtie2_genome"])

    statement = '''tmp=`mktemp -p %(tmp_dir)s`; checkpoint;
                   bowtie2 
                     --quiet 
                     --threads 12 
                     -x %(genome)s
                     -U %(infile)s 
                     %(options)s
                     1> $tmp 
                     2> %(log)s; checkpoint;
                   samtools sort -O BAM -o %(outfile)s $tmp; checkpoint;
                   rm $tmp''' % locals()

    print statement

    P.run()

@follows(mapBowtie2_PE, mapBowtie2_SE)
@transform("bowtie2.dir/*.genome.bam", suffix(r".genome.bam"), r".filt.bam")
def filterBam(infile, outfile):
    '''filter bams on MAPQ >10, & remove reads mapping to chrM before peakcalling'''

    job_memory = "5G"
    job_threads = "2"

    local_tmpdir = "$SCRATCH_DIR"

    statement = '''tmp=`mktemp -p %(local_tmpdir)s`; checkpoint ;
                   samtools view -h -q10 %(infile)s | 
                     grep -v "chrM" - | 
                     samtools view -h -o $tmp - ; checkpoint ;
                   samtools sort -O BAM -o %(outfile)s $tmp ; checkpoint ;
                   rm $tmp''' % locals()

    print statement

    P.run()


@transform(filterBam,
           regex(r"(.*).filt.bam"),
           r"\1.prep.bam")
def removeDuplicates(infile, outfile):
    '''PicardTools remove duplicates'''

    job_memory = "5G"
    job_threads = "2"
    
    metrics_file = outfile + ".picardmetrics"
    log = outfile + ".picardlog"
    tmp_dir = "$SCRATCH_DIR"

    statement = '''tmp=`mktemp -p %(tmp_dir)s`; checkpoint ; 
                   MarkDuplicates 
                     INPUT=%(infile)s 
                     ASSUME_SORTED=true 
                     REMOVE_DUPLICATES=true 
                     QUIET=true 
                     OUTPUT=$tmp 
                     METRICS_FILE=%(metrics_file)s 
                     VALIDATION_STRINGENCY=SILENT
                     2> %(log)s ; checkpoint ;
                   mv $tmp %(outfile)s; checkpoint ; 
                   samtools index %(outfile)s'''

    print statement

    P.run()

    
@follows(removeDuplicates)
@transform("bowtie2.dir/*.bam", suffix(r".bam"), r".bam.bai")
def indexBam(infile, outfile):
    '''index bams'''

    statement = '''samtools index -b %(infile)s > %(outfile)s'''

    P.run()

    
@follows(indexBam)
@transform("bowtie2.dir/*.bam", suffix(r".bam"), r".flagstats.txt")
def flagstatBam(infile, outfile):
    '''get samtools flagstats for bams'''

    statement = '''samtools flagstat %(infile)s > %(outfile)s'''

    print statement
    
    P.run()


@follows(flagstatBam)
@transform("bowtie2.dir/*.bam",
           regex(r"(.*).bam"),
           r"\1.picardAlignmentStats.txt")
def picardAlignmentSummary(infile, outfile):
    '''get aligntment summary stats with picard'''

    tmp_dir = "$SCRATCH_DIR"
    refSeq = os.path.join(PARAMS["annotations_genome_dir"], PARAMS["genome"] + ".fasta")

    job_threads = "4"
    job_memory = "5G"
    
    statement = '''tmp=`mktemp -p %(tmp_dir)s`; checkpoint ;
                   CollectAlignmentSummaryMetrics
                     R=%(refSeq)s
                     I=%(infile)s
                     O=$tmp; checkpoint ;
                   cat $tmp | grep -v "#" > %(outfile)s'''

    print statement

    P.run()


@merge(picardAlignmentSummary,
       "picardAlignmentSummary.load")
def loadpicardAlignmentSummary(infiles, outfile):
    '''load the complexity metrics to a single table in the db'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/(.*).picardAlignmentStats",
                         cat="sample_id",
                         options='-i "sample_id"')

    
@follows(loadpicardAlignmentSummary)
def mapping():
    pass


####################################################
#####              Peakcalling                 #####
####################################################
@follows(mapping, mkdir("macs2.dir"))
@transform("bowtie2.dir/*.prep.bam",
           regex("bowtie2.dir/(.*).prep.bam"),
           r"macs2.dir/\1.macs2.fragment_size.tsv")
def macs2Predictd(infile, outfile):
    '''predict fragment sizes for SE ChIP'''

    job_threads = "4"
    
    options = PARAMS["macs2_se_options"]
    outdir = os.path.dirname(outfile)

    if BamTools.isPaired(infile):
        statement = '''echo " " > %(outfile)s'''

    else:
        statement = '''macs2 predictd 
                         --format BAM 
                         --ifile %(infile)s 
                         --outdir %(outdir)s 
                         --verbose 2 %(options)s 
                         2> %(outfile)s'''

    print statement

    P.run()

    
@transform(macs2Predictd, suffix(r".fragment_size.tsv"), r".fragment_size.txt")
def getFragmentSize(infile, outfile):
    '''Get fragment sizes from macs2 predictd'''

    sample = os.path.basename(infile).rstrip(".fragment_size.tsv")
    tmp_dir = "$SCRATCH_DIR"
    
    statement = '''tmp=`mktemp -p %(tmp_dir)s`; checkpoint;
                   echo %(sample)s `cat %(infile)s | 
                     grep "# tag size =" | 
                     tr -s " " "\\t" | 
                     awk 'BEGIN {OFS="\\t "} {print $12}'` > $tmp; checkpoint; 
                   cat $tmp | tr -s " " "\\t" > %(outfile)s; checkpoint;
                   rm $tmp'''

    print statement

    P.run()

    
@transform(getFragmentSize, suffix(r".txt"), r".load")
def loadgetFragmentSize(infile, outfile):
    P.load(infile, outfile, options='-H "sample, tag_size"')

    
@follows(loadgetFragmentSize)
@transform(removeDuplicates,
           regex("bowtie2.dir/(.*).prep.bam"),
           add_inputs(r"macs2.dir/\1.macs2.fragment_size.txt"),
           r"macs2.dir/\1.macs2.log")
def macs2callpeaks(infiles, outfile):
    '''call peaks with macs2'''

    bam, fragment_size = infiles

    if BamTools.isPaired(bam):

        options = PARAMS["macs2_pe_options"]
        name = os.path.basename(outfile).split(".")[0]
        
        statement='''macs2 callpeak 
                       --format BAMPE
                       --outdir macs2.dir                  
                       %(options)s 
                       --treatment %(bam)s 
                       --name %(name)s 
                       >& %(outfile)s'''  

    else:
        # get macs2 predictd fragment lengths from csvdb
        table = os.path.basename(fragment_size).rstrip(".txt").replace("-", "_").replace(".", "_")

        query = '''select tag_size from %(table)s ''' % locals()

        dbh = sqlite3.connect(PARAMS["database"])
        cc = dbh.cursor()
        sqlresult = cc.execute(query).fetchall()

        fragment_length = sqlresult[0]
        fragment_length = fragment_length[0]

        # run macs2 callpeak
        job_threads = "5"

        options = PARAMS["macs2_se_options"]
        name = os.path.basename(outfile).split(".")[0]
        tmp_dir = "$SCRATCH_DIR"

        statement='''macs2 callpeak 
                       --outdir macs2.dir 
                       --nomodel 
                       --extsize 147                 
                       %(options)s 
                       --tsize %(fragment_length)s
                       --treatment %(bam)s 
                       --name %(name)s 
                       >& %(outfile)s'''  

    print statement
    
    P.run()

    
@follows(macs2callpeaks)
@files(None, "blacklist_chip.mm10.bed.gz")
def getChIPblacklist(infile, outfile):
    '''Get Ensembl ChIP blacklisted regions'''

    chip_blacklist = PARAMS["peak_filter_chip_blacklist"]
    statement = '''wget -O %(outfile)s %(chip_blacklist)s'''

    P.run()

    
@follows(getChIPblacklist)
@files(None, "blacklist_atac.mm10.bed.gz")
def getATACblacklist(infile, outfile):
    '''Get ATAC blacklist regions'''

    atac_blacklist = PARAMS["peak_filter_atac_blacklist"]
    statement = '''wget -q %(atac_blacklist)s | gzip - > %(outfile)s'''

    P.run()

    
@follows(getATACblacklist)
@transform("macs2.dir/*.narrowPeak",
           regex("(.*).narrowPeak"),
           add_inputs([getChIPblacklist, getATACblacklist]),
           r"\1.filt.bed")
def filterPeaks(infiles, outfile):
    '''subtract blacklist regions from peakset'''

    peak, blacklists = infiles

    blacklist = ' '.join(blacklists)
    
    statement = '''intersectBed -wa -v -a %(peak)s -b <(zcat %(blacklist)s ) > %(outfile)s'''

    print statement
    
    P.run()

    
@follows(filterPeaks)
def peakcalling():
    pass


########################################################
####              Coverage tracks                   ####
########################################################
@follows(peakcalling, mkdir("deeptools.dir"))
@transform("bowtie2.dir/*.prep.bam",
           regex(r"(.*).bam"),
           r"\1.bam.bai")
def indexPrepBam(infile, outfile):
    '''samtools index bam'''

    statement = '''samtools index -b %(infile)s %(outfile)s'''

    P.run()

    
@follows(indexPrepBam)
@transform("bowtie2.dir/*.prep.bam",
           regex(r"bowtie2.dir/(.*).prep.bam"),
           r"deeptools.dir/\1.coverage.bw")
def bamCoverage(infile, outfile):
    '''Make normalised bigwig tracks with deeptools'''

    job_memory = "2G"
    job_threads = "10"

    if BamTools.isPaired(infile):

        # PE reads filtered on sam flag 66 -> include only first read in properly mapped pairs
        
        statement = '''bamCoverage -b %(infile)s -o %(outfile)s
                    --binSize 5
                    --smoothLength 20
                    --centerReads
                    --normalizeUsingRPKM
                    --samFlagInclude 66
                    -p "max"'''
        
    else:

        # SE reads filtered on sam flag 4 -> exclude unmapped reads
        
        statement = '''bamCoverage -b %(infile)s -o %(outfile)s
                    --binSize 5
                    --smoothLength 20
                    --centerReads
                    --normalizeUsingRPKM
                    --samFlagExclude 4
                    -p "max"'''

    # added smoothLength = 20 to try and get better looking plots...
    # --minMappingQuality 10 optional argument, but unnecessary as bams are alredy filtered
    # centerReads option and small binsize should increase resolution around enriched areas

    print statement
    
    P.run()

# @follows(bamCoverage)
# @transform("deeptools.dir/*_1.coverage.bw",
#            regex(r"(.*)_1.coverage.bw"),
#            r"\1.merge.bdg")
# def mergeBWreps(infile, outfile):
#     '''merge replicates to one BW for IGV visualisation'''

#     bw1 = infile
#     bw2 = bw1.replace("_1.coverage.bw", "_2.coverage.bw")
#     bw3 = bw1.replace("_1.coverage.bw", "_3.coverage.bw")

#     tmp_dir = "$SCRATCH_DIR"
    
#     statement = '''tmp=`mktemp -p %(tmp_dir)s`; checkpoint;
#                    bigWigMerge %(bw1)s %(bw2)s %(bw3)s $tmp; checkpoint;
#                    sort -k1,1 -k2,2n $tmp > %(outfile)s'''

#     print statement

#     P.run()

# @transform(mergeBWreps, suffix(r".bdg"), r".bw")
# def BW2BDG(infile, outfile):
#     '''convert bedgraph back to bigwig'''

#     chrom_sizes = PARAMS["annotations_chrom_sizes"]
    
#     statement = '''bedGraphToBigWig %(infile)s %(chrom_sizes)s %(outfile)s'''

#     print statement

#     P.run()


@follows(bamCoverage)
def coverage():
    pass


# ---------------------------------------------------
# Generic pipeline tasks
@follows(mapping, peakcalling, coverage)
def full():
    pass


@follows(mkdir("report"))
def build_report():
    '''build report from scratch.

    Any existing report will be overwritten.
    '''

    E.info("starting report build process from scratch")
    P.run_report(clean=True)


@follows(mkdir("report"))
def update_report():
    '''update report.

    This will update a report with any changes inside the report
    document or code. Note that updates to the data will not cause
    relevant sections to be updated. Use the cgatreport-clean utility
    first.
    '''

    E.info("updating report")
    P.run_report(clean=False)


@follows(update_report)
def publish_report():
    '''publish report in the CGAT downloads directory.'''

    E.info("publishing report")
    P.publish_report()

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
