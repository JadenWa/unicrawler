# -*- coding: utf-8 -*-
__author__ = 'yijingping'
# 加载django环境
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ['DJANGO_SETTINGS_MODULE'] = 'unicrawler.settings'
import django
django.setup()

import json
from lxml import etree
from cores.constants import KIND_LIST_URL, KIND_DETAIL_URL
from io import StringIO
from django.conf import settings
from cores.util import get_redis, get_uniqueid
from cores.extractors import XPathExtractor, PythonExtractor, ImageExtractor
import logging
logger = logging.getLogger()


class Extractor(object):
    def __init__(self):
        self.redis = get_redis()

    def extract(self, tree, rules):
        res = []
        for rule in rules:
            extractor = None
            if rule["kind"] == "xpath":
                extractor = XPathExtractor(tree, rule["data"])
            elif rule["kind"] == "python":
                extractor = PythonExtractor(rule["data"], res)
            elif rule["kind"] == "image":
                extractor = ImageExtractor(res)

            res = extractor.extract()

        return res

    def check_detail_fresh_time(self, data, unique_key, fresh_time):
        if fresh_time <= 0:
            return False
        else:
            print data, unique_key, fresh_time
            unique_value = ''.join([str(data.get(item)) for item in unique_key])
            key = 'unicrawler:detail_fresh_time:%s' % get_uniqueid(unique_value)
            if self.redis.exists(key):
                return True
            else:
                self.redis.setex(key, fresh_time, fresh_time)
                return False

    def get_detail(self, tree, data):
        # 检查是否在exclude规则内. 如果在,放弃存储
        exclude_rules = data['detail_exclude']
        if self.extract(tree, exclude_rules):
            logger.debug('# url in excludes, abort!')
            return

        # 不在exclude规则内,可以存储
        result = {
            "url": data['url'],
            "seed_id": data['seed_id'],
            'detail_multi': data['detail_multi']
        }
        rules = data['detail_rules']
        for item in rules:
            col = item["key"]
            print col
            col_rules = item["rules"]
            col_value = self.extract(tree, col_rules)
            result[col] = col_value


        # 检查多项详情新鲜度
        if data['detail_multi'] and self.check_detail_fresh_time(result, data['unique_key'], data["detail_fresh_time"]):
            # 未过期,不更新
            logger.info('检查多项详情未过期,不更新')
        else:
            # 已过期,更新
            self.redis.lpush(settings.CRAWLER_CONFIG["processor"], json.dumps(result))
            logger.debug('extracted:%s' % result)

    def run(self):
        r = get_redis()
        #if settings.DEBUG:
        #    r.delete(settings.CRAWLER_CONFIG["extractor"])
        while True:
            try:
                data = r.brpop(settings.CRAWLER_CONFIG["extractor"])
            except Exception as e:
                print e
                continue
            #print data
            data = json.loads(data[1])
            body = data['body']
            htmlparser = etree.HTMLParser()
            tree = etree.parse(StringIO(body), htmlparser)
            # 1 如果当前接卸的页面是列表页
            if data["kind"] == KIND_LIST_URL:
                # 1.1先找详情页
                # 检查详情的内容是否都包含在列表页中
                multi_rules = data['detail_multi']
                if multi_rules:
                    # 1.1.1 详情都包含在列表页中
                    multi_parts = self.extract(tree, multi_rules)
                    for part in multi_parts:
                        tree = etree.parse(StringIO(part), htmlparser)
                        self.get_detail(tree, data)
                else:
                    # 1.1.2 详情不在列表中,通过列表url去访问详情
                    detail_urls = self.extract(tree, data['list_rules'])
                    #logger.debug('detail_urls: %s' % detail_urls)
                    for item in detail_urls:
                        item_data = {
                            "url": item,
                            'kind': KIND_DETAIL_URL,
                            'seed_id': data['seed_id'],
                            'rule_id': data['rule_id'],
                            #'fresh_pages': '',
                            #'list_rules': '',
                            #'next_url_rules': '',
                            'site_config': data['site_config'],
                            'detail_rules': data['detail_rules'],
                            'detail_exclude': data['detail_exclude'],
                            'detail_multi': data['detail_multi'],
                            'detail_fresh_time': data['detail_fresh_time'],
                            'unique_key': data['unique_key']
                        }
                        r.lpush(settings.CRAWLER_CONFIG["downloader"], json.dumps(item_data))

                # 1.2后找下一页
                next_urls = self.extract(tree, data["next_url_rules"])
                print 'next_urls: %s' % next_urls
                for item in next_urls:
                    item_data = {
                        "url": item,
                        'kind': KIND_LIST_URL,
                        'seed_id': data['seed_id'],
                        'rule_id': data['rule_id'],
                        'fresh_pages': data['fresh_pages'] - 1,
                        'site_config': data['site_config'],
                        'list_rules': data['list_rules'],
                        'next_url_rules': data['next_url_rules'],
                        'detail_rules': data['detail_rules'],
                        'detail_exclude': data['detail_exclude'],
                        'detail_multi': data['detail_multi'],
                        'detail_fresh_time': data['detail_fresh_time'],
                        'unique_key': data['unique_key']
                    }
                    if item_data['fresh_pages'] > 0:
                        logger.debug('list:%s' % data['url'])
                        r.lpush(settings.CRAWLER_CONFIG["downloader"], json.dumps(item_data))
            # 2 如果当前解析的页面是详情页
            elif data["kind"] == KIND_DETAIL_URL:
                logger.debug('detail:%s' % data['url'])
                # 如果没有多项详情,则只是单项
                self.get_detail(tree, data)


if __name__ == '__main__':
    my_extractor = Extractor()
    my_extractor.run()
